import struct, numpy as np
def load(path='/mnt/user-data/uploads/the_dipper_man_-_the_dipper_man.xm'):
    d=open(path,'rb').read()
    hs,songlen,restart,nch,npat,ninst,flags,speed,bpm=struct.unpack('<IHHHHHHHH',d[60:80])
    order=list(d[80:80+songlen])
    pos=60+hs; pats=[]
    for p in range(npat):
        hl,pt,rows,ps=struct.unpack('<IBHH',d[pos:pos+9])
        q=pos+hl; grid=[]
        for r in range(rows):
            row=[]
            for c in range(nch):
                b=d[q]
                if b&0x80:
                    q+=1; note=ins=vol=eff=par=0
                    if b&1: note=d[q];q+=1
                    if b&2: ins=d[q];q+=1
                    if b&4: vol=d[q];q+=1
                    if b&8: eff=d[q];q+=1
                    if b&16: par=d[q];q+=1
                else:
                    note,ins,vol,eff,par=d[q:q+5];q+=5
                row.append((note,ins,vol,eff,par))
            grid.append(row)
        pats.append(grid); pos+=hl+ps
    insts=[]
    for i in range(ninst):
        isz,=struct.unpack('<I',d[pos:pos+4]); ns,=struct.unpack('<H',d[pos+27:pos+29])
        kmap=list(d[pos+33:pos+129]) if ns else [0]*96
        p2=pos+isz; sm=[]
        shs=struct.unpack('<I',d[pos+29:pos+33])[0] if ns else 0
        hdrs=[]
        for s in range(ns):
            ln,ls,ll,vol,ft,ty,pan,rn,_=struct.unpack('<IIIBbBBbB',d[p2:p2+18]); hdrs.append((ln,ls,ll,vol,ft,ty,pan,rn)); p2+=shs
        for h in hdrs:
            ln=h[0]; raw=d[p2:p2+ln]; p2+=ln
            if h[5]&16:
                x=np.frombuffer(raw,'<i2').astype(np.int64); x=np.cumsum(x); x=((x+32768)%65536)-32768
            else:
                x=np.frombuffer(raw,'<i1').astype(np.int64); x=np.cumsum(x); x=((x+128)%256)-128; x=x*256
            sm.append((h,x.astype(np.float64)))
        insts.append(sm); pos=p2
    return dict(order=order,pats=pats,insts=insts,nch=nch,speed=speed,bpm=bpm)
