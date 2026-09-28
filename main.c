// GBA music demo: shapes that bounce to the beat of an .xm song.
//
// How it works, in plain words:
//  - Maxmod (a music library) plays your .xm song.
//  - beatmap.h says which rows of the song have drums, bass or melody.
//  - We count song rows on our own, then react to each one.
//
// Things you can safely change are marked with  <-- TWEAK

#include <stdint.h>
#include <gba_interrupt.h>
#include <gba_systemcalls.h>
#include <maxmod.h>
#include "soundbank.h"   // made by the build (gives us MOD_SONG)
#include "beatmap.h"     // the beat map

extern const unsigned char soundbank_bin[];

// ---------- Hardware addresses ----------
#define HW_DISPCNT   (*(volatile uint16_t *)0x04000000)
#define HW_PAL_BG    ((volatile uint16_t *)0x05000000)
#define HW_PAL_OBJ   ((volatile uint16_t *)0x05000200)
#define HW_OBJ_TILES ((volatile uint16_t *)0x06010000)
#define HW_OAM       ((volatile uint16_t *)0x07000000)

#define RGB15(r, g, b) ((r) | ((g) << 5) | ((b) << 10))

// ---------- Timing ----------
// This song: speed 6, tempo 125 -> one row = 0.12 seconds.
// The GBA draws 59.7275 frames a second, so one row = 7.1673 frames.
// We store that number times 10000 so we can use whole numbers.
#define ROW_X 71673

// If the shapes hit LATE, make this smaller. If EARLY, make it bigger.
#define START_DELAY_FRAMES 2   // <-- TWEAK

// ---------- Audio ----------
// The GBA mixes all song channels in software into ONE 8-bit stream. If the
// channels add up to more than 8 bits can hold, the sound "clips" and crackles.
// The song file (the_dipper_man_gba.xm) has been levelled so every channel is
// audible; this master volume (0-1024) leaves headroom for when many channels
// play at once. Too crackly / distorted? make it smaller. Too quiet? make it
// bigger (about 700 is the most you can use before loud passages clip again).
#define MASTER_VOLUME 512      // <-- TWEAK
#define SONG_CHANNELS 10       // the song uses 10 tracker channels

// ---------- Colors ----------
static const uint16_t colors[8] = {
    RGB15(31, 4, 6),   RGB15(31, 20, 0), RGB15(31, 30, 2), RGB15(4, 28, 8),
    RGB15(2, 26, 31),  RGB15(6, 10, 31), RGB15(20, 6, 30), RGB15(31, 8, 22)
};

static const uint8_t bgbase[4][3] = { {1, 1, 7}, {7, 1, 5}, {1, 6, 4}, {6, 4, 1} };

// ---------- Song clock and effects ----------
static int row = NUM_ROWS - 1;
static int acc = ROW_X;
static int delay = START_DELAY_FRAMES;

static int pulse = 0;      // big shapes grow on the beat, then shrink back
static int spulse = 0;     // small shapes grow on drum hits
static int flash = 0;      // background flash
static int tint = 0;       // background color, changes on melody hits
static int small_step = 0; // small shape colors change on drum hits
static int big_step = 0;   // big shape colors change on melody hits

// The small shapes drift around and bounce off the walls, like a DVD logo
static int sx[3] = { 40, 120, 190 };
static int sy[3] = { 30, 60, 40 };
static int svx[3] = { 1, -2, 2 };   // <-- TWEAK (speeds)
static int svy[3] = { 2, 1, -1 };

// The big shapes hop on the beat. Each has its own hop height.
static const int big_x[3] = { 48, 120, 192 };
static const int big_amp[3] = { 44, 32, 24 };  // <-- TWEAK
#define GROUND_Y 108

// ---------- Making the shapes ----------
// Three shapes, 32x32 pixels each: circle, square, diamond.
static uint16_t tilebuf[3 * 16 * 16];

static void put_pixel(int shape, int px, int py, int color) {
    int tile = (py >> 3) * 4 + (px >> 3);
    int q = (py & 7) * 8 + (px & 7);
    tilebuf[(shape * 16 + tile) * 16 + (q >> 2)] |= (uint16_t)(color << ((q & 3) * 4));
}

static void build_shapes(void) {
    for (int py = 0; py < 32; py++) {
        for (int px = 0; px < 32; px++) {
            int dx = 2 * px - 31;
            int dy = 2 * py - 31;
            int adx = dx < 0 ? -dx : dx;
            int ady = dy < 0 ? -dy : dy;

            int d2 = dx * dx + dy * dy;                       // circle
            if (d2 <= 31 * 31) put_pixel(0, px, py, d2 > 26 * 26 ? 2 : 1);

            int m = adx > ady ? adx : ady;                    // square
            put_pixel(1, px, py, m > 25 ? 2 : 1);

            int s = adx + ady;                                // diamond
            if (s <= 31) put_pixel(2, px, py, s > 26 ? 2 : 1);
        }
    }
    for (int i = 0; i < 3 * 16 * 16; i++) HW_OBJ_TILES[i] = tilebuf[i];
}

// ---------- Sprites ----------
// Sprite i uses its own "scaling group" i, so each can grow on its own.
static void set_sprite(int i, int cx, int cy, int shape, int bank, int scale256) {
    int pa = 65536 / scale256;
    HW_OAM[i * 4 + 0] = (uint16_t)(((cy - 32) & 0xFF) | 0x0300);
    HW_OAM[i * 4 + 1] = (uint16_t)(((cx - 32) & 0x1FF) | (i << 9) | (2 << 14));
    HW_OAM[i * 4 + 2] = (uint16_t)((shape * 16) | (bank << 12));
    HW_OAM[i * 16 + 3] = (uint16_t)pa;
    HW_OAM[i * 16 + 7] = 0;
    HW_OAM[i * 16 + 11] = 0;
    HW_OAM[i * 16 + 15] = (uint16_t)pa;
}

// ---------- Reacting to a song row ----------
static void on_row(int r) {
    unsigned char f = rowmap[r];
    if ((r & 3) == 0) { pulse = 110; flash = 14; }     // the beat
    if (f & 2) { if (pulse < 70) pulse = 70; }         // bass
    if (f & 1) { spulse = 120; small_step++; }         // drums
    if (f & 4) { tint = (tint + 1) & 3; big_step++; }  // melody
}

// ---------- Drawing (runs right after the screen refreshes) ----------
static void draw(void) {
    // Background
    int r = bgbase[tint][0] + flash; if (r > 31) r = 31;
    int g = bgbase[tint][1] + flash; if (g > 31) g = 31;
    int b = bgbase[tint][2] + flash; if (b > 31) b = 31;
    HW_PAL_BG[0] = RGB15(r, g, b);

    // Shape colors
    for (int i = 0; i < 3; i++) {
        HW_PAL_OBJ[i * 16 + 1] = colors[(big_step + i * 3) & 7];
        HW_PAL_OBJ[(3 + i) * 16 + 1] = colors[(small_step * 3 + i * 2) & 7];
    }

    // How far through the current beat are we? (0 to 255)
    int pos = (row & 3) * ROW_X + acc;
    if (pos > 4 * ROW_X - 1) pos = 4 * ROW_X - 1;
    int p = (pos * 256) / (4 * ROW_X);
    int hop = (p * (256 - p)) >> 8;   // 0 at the beat, 64 in the middle

    for (int i = 0; i < 3; i++) {
        int y = GROUND_Y - ((hop * big_amp[i]) >> 6);
        set_sprite(i, big_x[i], y, i, i, 256 + pulse);
    }
    for (int i = 0; i < 3; i++) {
        set_sprite(3 + i, sx[i], sy[i], (i + 1) % 3, 3 + i, 128 + spulse);
    }
}

// ---------- Updating (runs once per frame) ----------
static void update(void) {
    if (delay > 0) {
        delay--;
    } else {
        acc += 10000;
        while (acc >= ROW_X) {
            acc -= ROW_X;
            row++;
            if (row >= NUM_ROWS) row = 0;
            on_row(row);
        }
    }

    if (pulse > 0) { pulse -= (pulse >> 2) + 1; if (pulse < 0) pulse = 0; }
    if (spulse > 0) { spulse -= (spulse >> 2) + 1; if (spulse < 0) spulse = 0; }
    if (flash > 0) flash--;

    for (int i = 0; i < 3; i++) {
        sx[i] += svx[i];
        sy[i] += svy[i];
        if (sx[i] < 14)  { sx[i] = 14;  svx[i] = -svx[i]; }
        if (sx[i] > 226) { sx[i] = 226; svx[i] = -svx[i]; }
        if (sy[i] < 14)  { sy[i] = 14;  svy[i] = -svy[i]; }
        if (sy[i] > 146) { sy[i] = 146; svy[i] = -svy[i]; }
    }
}

int main(void) {
    HW_DISPCNT = 0x0080;   // screen off while we set things up

    irqInit();
    irqSet(IRQ_VBLANK, mmVBlank);
    irqEnable(IRQ_VBLANK);
    mmInitDefault((mm_addr)soundbank_bin, SONG_CHANNELS);

    for (int i = 0; i < 128; i++) HW_OAM[i * 4] = 0x0200;  // hide all sprites
    build_shapes();
    for (int bank = 0; bank < 6; bank++) HW_PAL_OBJ[bank * 16 + 2] = RGB15(31, 31, 31);

    HW_DISPCNT = 0x1040;   // sprites on, 1D tile layout, screen on

    mmStart(MOD_SONG, MM_PLAY_LOOP);
    mmSetModuleVolume(MASTER_VOLUME);   // headroom so channels don't clip each other

    while (1) {
        VBlankIntrWait();
        draw();
        update();
        mmFrame();
    }
}
