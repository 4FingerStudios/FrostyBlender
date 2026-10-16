"""Convert decoded Skate/ReSkate mod assets into Aura meshes and a scene manifest.
All outputs are local game assets. Never commit them to the plugin repository.
"""
import argparse, hashlib, json, math, re, struct, zlib
from pathlib import Path
import numpy as np
import os
from concurrent.futures import ThreadPoolExecutor
from collections import deque

TYPE_SIZES={0x2a36e9d5:8,0x36ed3444:4,0x885eff59:12,0x57f970ca:4,
            0x7826e998:4,0x7f999829:16,0x2bb65c2d:4,0x8e4a817e:4}
TEXTURE_TYPE=0x14c5fc30

def png(path,width,height,pixels):
    def chunk(tag,data):return struct.pack('>I',len(data))+tag+data+struct.pack('>I',zlib.crc32(tag+data)&0xffffffff)
    raw=b''.join(b'\0'+pixels[y*width*4:(y+1)*width*4] for y in range(height))
    path.parent.mkdir(parents=True,exist_ok=True)
    path.write_bytes(b'\x89PNG\r\n\x1a\n'+chunk(b'IHDR',struct.pack('>IIBBBBB',width,height,8,6,0,0,0))+chunk(b'IDAT',zlib.compress(raw,3))+chunk(b'IEND',b''))

def shader_blob(data,start,size):
    end=start+size
    if start<0 or size<4 or end>len(data):raise ValueError('Shader packet bounds invalid')
    count=struct.unpack_from('<I',data,start)[0];at=start+4;out={}
    if count>1024:raise ValueError('Shader packet bounds invalid')
    for _ in range(count):
        if at+16>end:raise ValueError('Shader parameter header truncated')
        ph,typ,flags,hi=struct.unpack_from('<QIHH',data,at);at+=16
        name=(hi<<16)|(ph>>48)
        if typ==TEXTURE_TYPE:
            if at>=end:raise ValueError('Shader texture count truncated')
            n=data[at];at+=1;value=[]
            for _ in range(n):
                value.append({'object':data[at:at+16].hex(),'file':data[at+16:at+32].hex()});at+=32
        else:
            if at+4>end:raise ValueError('Shader scalar count truncated')
            n=struct.unpack_from('<I',data,at)[0];at+=4
            if n>1024 or typ not in TYPE_SIZES:raise ValueError(f'Unknown shader value type {typ:08x}')
            length=n*TYPE_SIZES[typ];raw=data[at:at+length];at+=length
            value={'raw':raw.hex(),'count':n,'type':typ}
        if at>end:raise ValueError('Shader packet overrun')
        out[f'{name:08x}']=value
    if at!=end:raise ValueError('Shader packet end mismatch')
    return out

class Archive:
    def __init__(self,root):
        self.root=Path(root);self.inventory=json.loads((self.root/'inventory.json').read_text())
        self.assets=self.inventory['assets'];self.documents={};self.resources={};self.chunks={};self.by_guid={}
        self.shaders={}
        for a in self.assets:
            if 'bytes' not in a:continue
            if a['kind']=='resource':self.resources[a['resource_id']]=a
            if a['kind']=='chunk':self.chunks[a['guid']]=a
            if 'document' in a:
                doc=json.loads((self.root/a['document']).read_text());self.documents[a['name']]=(a,doc);self.by_guid[doc['guid']]=(a,doc)
        for a in self.resources.values():
            if a['resource_type']!=0x73312045:continue
            data=self.data(a);count=struct.unpack('<4I',bytes(a['metadata']))[3]
            if data[:16]!=struct.pack('<QQ',16,0):raise ValueError('Unsupported ShaderBlockDepot layout')
            for i in range(count):
                key,ptr=struct.unpack_from('<QQ',data,16+i*16);offset,h,size=struct.unpack_from('<QQQ',data,ptr)
                self.shaders[key]=shader_blob(data,offset,size)
    def data(self,a):return (self.root/a['path']).read_bytes()
    def resolve(self,doc,p):
        if not p or p.get('$pointer',0)==0:return None
        if p['$pointer']==1:return (doc,doc['objects'][p['index']])
        imp=doc['imports'][p['index']];other=self.by_guid.get(imp['file'])
        if not other:return None
        d=other[1]
        obj=next((o for o in d['objects'] if o['$guid']==imp['object']),None)
        return (d,obj) if obj else None

def matrix(value):
    m=np.eye(4,dtype=np.float64)
    for i,k in enumerate(['right','up','forward','trans']):
        v=value.get(k,value.get(k.capitalize(),{}));m[:3,i]=[v.get(c,0) for c in ['x','y','z']]
    return m

def read_attr(geom,base,count,stride,fmt,offset):
    shapes={1:('f4',1),2:('f4',2),3:('f4',3),4:('f4',4),5:('f2',1),6:('f2',2),7:('f2',3),8:('f2',4),12:('u1',4),13:('u1',4),23:('u2',4)}
    if fmt not in shapes:raise ValueError(f'Unsupported vertex format {fmt}')
    typ,n=shapes[fmt];size=np.dtype(typ).itemsize
    if base+offset+(count-1)*stride+n*size>len(geom):raise ValueError('Vertex attribute overrun')
    values=np.ndarray((count,n),dtype='<'+typ,buffer=geom,offset=base+offset,strides=(stride,size)).astype(np.float32)
    if fmt==13:values/=255
    return values

def mesh_sections(archive,res):
    data=archive.data(res);header=struct.unpack_from('<4I',data)
    if header not in [(192,176,384,0),(208,188,384,0)]:raise ValueError(f'Unsupported MeshSet layout {header}')
    lod=struct.unpack_from('<Q',data,48)[0]+16
    count=struct.unpack_from('<I',data,lod+8)[0];table=struct.unpack_from('<Q',data,lod+12)[0]+16
    fmt,vindex,vsize=struct.unpack_from('<III',data,lod+84)
    guid=data[lod+116:lod+132].hex();chunk=archive.chunks.get(guid)
    if not chunk:raise ValueError('Geometry chunk missing: '+guid)
    geom=archive.data(chunk)
    if len(geom)<vindex+vsize:raise ValueError('Geometry chunk too short')
    results=[]
    for i in range(count):
        s=table+i*384;prim=data[s+31];tri,first,voff,vc=struct.unpack_from('<4I',data,s+32)
        if not vc or not tri:continue
        if prim!=3:raise ValueError(f'Unsupported primitive {prim}')
        name_at=struct.unpack_from('<Q',data,s+8)[0]+16
        name=data[name_at:data.index(b'\0',name_at)].decode('utf8')
        # Native shadow-only sections are depth proxies, not colored surfaces.
        if name.lower().endswith('_shadow'):continue
        decl=s+112;streams=data[decl+97];elements=data[decl+96]
        base=voff;bases=[];strides=[]
        for j in range(streams):
            stride=data[decl+64+j*2];bases.append(base);strides.append(stride);base+=stride*vc
        if base>vsize:raise ValueError('Vertex streams exceed buffer')
        attrs={}
        for j in range(elements):
            usage,af,off,stream=data[decl+j*4:decl+j*4+4]
            if usage in [1,6,33,34,37,51,52]:attrs[usage]=read_attr(geom,bases[stream],vc,strides[stream],af,off)
        dtype='<u2' if fmt==33 else '<u4' if fmt==32 else None
        if not dtype:raise ValueError(f'Unsupported index format {fmt}')
        indices=np.frombuffer(geom,dtype=dtype,count=tri*3,offset=vsize+first*np.dtype(dtype).itemsize).astype(np.uint32)
        if indices.max()>=vc:raise ValueError('Section indices are not local')
        pos=attrs[1][:,:3].copy();uv=attrs.get(33,np.zeros((vc,2),np.float32))[:,:2].copy()
        normal=attrs.get(6)
        if normal is None:
            normal=np.zeros((vc,3),np.float32);tris=indices.reshape(-1,3)
            face=np.cross(pos[tris[:,1]]-pos[tris[:,0]],pos[tris[:,2]]-pos[tris[:,0]])
            for j in range(3):np.add.at(normal,tris[:,j],face)
            lengths=np.linalg.norm(normal,axis=1);normal/=np.maximum(lengths,1e-12)[:,None];normal[lengths<1e-12]=[0,1,0]
        # Usage 52 is a packed tangent frame, not vertex color.
        color=np.ones((vc,3),np.float32)
        results.append({'name':name,'positions':pos,'uv':uv,'normals':normal[:,:3], 'color':color,
                        'palette':attrs.get(51,np.zeros((vc,4),np.float32))[:,0],
                        'indices':indices,'state_key':struct.unpack_from('<Q',data,s+312)[0]})
    return results

def physics_sections(archive,res):
    data=archive.data(res)
    version,magic,bodies,parts=struct.unpack_from('<4I',data)
    shape_count=struct.unpack_from('<I',data,24)[0]
    if version!=2 or magic!=0x69af7015 or bodies!=1 or not 0<parts<=1024:
        raise ValueError('Unsupported PhysicsResource body/part layout')
    shape_table=40+struct.unpack_from('<I',data,40)[0];part_table=48+struct.unpack_from('<I',data,48)[0]
    result=[]
    for part_index in range(parts):
        part=part_table+part_index*72
        if struct.unpack_from('<I',data,part+36)[0]!=5:raise ValueError('Physics shape is not a triangle mesh')
        shape_index=struct.unpack_from('<I',data,part+56)[0]
        if shape_index>=shape_count:raise ValueError('Physics part shape index invalid')
        shape=shape_table+shape_index*84
        nv,ni=struct.unpack_from('<2H',data,shape+24)
        vp=shape+36+struct.unpack_from('<I',data,shape+36)[0]
        ip=shape+48+struct.unpack_from('<I',data,shape+48)[0]
        pos=np.frombuffer(data,dtype='<f4',count=nv*3,offset=vp).reshape(-1,3).copy()
        indices=np.frombuffer(data,dtype='<u2',count=ni,offset=ip).astype(np.uint32)
        if ni%3 or indices.max()>=nv or not np.isfinite(pos).all():raise ValueError('Physics triangles invalid')
        q=np.array(struct.unpack_from('<4f',data,part));qn=np.linalg.norm(q)
        if not np.isfinite(qn) or qn<1e-8:raise ValueError('Physics rotation invalid')
        q/=qn
        u=q[:3];t=2*np.cross(u,pos);pos+=q[3]*t+np.cross(u,t)
        pos+=np.array(struct.unpack_from('<3f',data,part+16))
        result.append({'name':'native_collision_'+str(part_index),'positions':pos,'indices':indices,'normals':np.tile([0,1,0],(nv,1)),
                 'uv':np.zeros((nv,2)),'color':np.ones((nv,3)),'palette':np.zeros(nv),'state_key':0})
    return result

def write_mesh(path,sections):
    vertices=[];indices=[];parts=[];strings=bytearray(b'\0');base=0;first=0
    for i,s in enumerate(sections):
        pos=s['positions'];normal=s['normals'];uv=s['uv'];col=s['color'];idx=s['indices']
        data=np.zeros((len(pos),12),dtype='<f4');data[:,:3]=pos;data[:,3:6]=normal;data[:,6:9]=col;data[:,9]=s['palette'];data[:,10:12]=uv
        vertices.append(data);indices.append(idx+base)
        name=len(strings);strings+=s['name'].encode()+b'\0'
        parts.append(struct.pack('<4I6f',first,len(idx),i,name,*pos.min(axis=0),*pos.max(axis=0)))
        base+=len(pos);first+=len(idx)
    vv=np.concatenate(vertices);ii=np.concatenate(indices).astype('<u4')
    header=struct.pack('<16I6f',0x48534d41,1,6,48,len(vv),len(ii),len(parts),0,0,0,0,0,0,len(strings),0,0,*vv[:,:3].min(axis=0),*vv[:,:3].max(axis=0))
    data=bytearray()
    for block in [header,vv.tobytes(),ii.tobytes(),b''.join(parts),bytes(strings)]:
        data+=b'\0'*((-len(data))%16);data+=block
    data+=b'\0'*((-len(data))%16)
    path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(data)
    return len(vv),len(ii)//3

def write_skin(path,sections):
    """Bone weights of a skinned mesh, beside its AMSH in the same vertex order: b'ASKN', version 1,
    vertex count, influences per vertex, then u16 skeleton bone indices and f32 weights."""
    skins=[s.get('skin') for s in sections]
    if not any(skins):
        if path.exists():path.unlink()
        return False
    width=max(k[0].shape[1] for k in skins if k)
    bones=[];weights=[]
    for s,k in zip(sections,skins):
        b=np.zeros((len(s['positions']),width),'<u2');w=np.zeros((len(s['positions']),width),'<f4')
        if k:b[:,:k[0].shape[1]]=k[0];w[:,:k[1].shape[1]]=k[1]
        bones.append(b);weights.append(w)
    b=np.concatenate(bones);w=np.concatenate(weights)
    path.parent.mkdir(parents=True,exist_ok=True)
    path.write_bytes(b'ASKN'+struct.pack('<3I',1,len(b),width)+b.tobytes()+w.tobytes())
    return True

def skin_attrs(attrs,palette,count):
    """Skeleton bone indices and normalised weights from blend index (2, 3) and weight (4, 5)
    streams. With a palette the indices select from the section's bone list (Garden Warfare);
    without one they are bone ids already (Battle for Neighborville, where ids with bit 15 set
    name the mesh's own procedural bones)."""
    if 2 not in attrs or 4 not in attrs:return None
    local=np.concatenate([attrs[u] for u in (2,3) if u in attrs],axis=1).astype(np.int64)
    weights=np.concatenate([attrs[u] for u in (4,5) if u in attrs],axis=1)
    width=min(local.shape[1],weights.shape[1]);local,weights=local[:,:width],weights[:,:width].astype(np.float32)
    weights[weights<0]=0
    used=weights>0
    if palette is None:bones=np.where(used,local,0)
    else:
        if not len(palette) or (local[used]>=len(palette)).any():raise ValueError('Skinned section bone index outside its palette')
        bones=np.asarray(palette,np.int64)[np.where(used,local,0)];bones[~used]=0
    total=weights.sum(axis=1,keepdims=True);weights=np.where(total>0,weights/np.maximum(total,1e-12),0)
    return bones.astype('<u2'),weights.astype('<f4')

class Converter:
    def __init__(self,archive,textures,assets):
        self.archive=archive;self.texdir=Path(textures);self.texture_info=json.loads((self.texdir/'textures.json').read_text())['textures']
        self.workers=max(1,min(8,int(os.environ.get('AURA_IMPORT_JOBS',min(4,os.cpu_count() or 1)))))
        self.png_pool=ThreadPoolExecutor(max_workers=self.workers)
        self.png_pending=deque()
        self.assets=Path(assets);self.materials={};self.meshes={};self.texture_paths={};self.errors=[];self.collision_geometry=[];self.counts={'triangles':0,'vertices':0,'shader_sections':0,'missing_shader_sections':0}
    def texture(self,guid):
        if guid in self.texture_paths:return self.texture_paths[guid]
        found=self.archive.by_guid.get(guid)
        if not found:return None
        obj=found[1]['objects'][0];rid=obj.get('Resource',{}).get('$resource');info=self.texture_info.get(rid)
        if not info:return None
        raw=(self.texdir/info['path']).read_bytes();w,h=struct.unpack_from('<II',raw)
        if len(self.png_pending)>=self.workers*2:self.png_pending.popleft().result()
        rel='textures/frostbite/'+guid+'.png'
        self.png_pending.append(self.png_pool.submit(png,self.assets/rel,w,h,raw[8:]))
        self.texture_paths[guid]=rel
        return rel
    def finish_textures(self):
        try:
            while self.png_pending:self.png_pending.popleft().result()
        finally:self.png_pool.shutdown(wait=True)
    def material(self,key,name):
        packet=self.archive.shaders.get(key)
        if packet is None:
            self.counts['missing_shader_sections']+=1;self.errors.append({'stage':'material','name':name,'state_key':key,'error':'No compiled shader packet'})
            packet={}
        else:self.counts['shader_sections']+=1
        digest=hashlib.sha256(json.dumps(packet,sort_keys=True).encode()).hexdigest()[:20]
        if digest in self.materials:return self.materials[digest]
        paths={};missing=[];bindings={}
        # These slot hashes are verified against the referenced TextureAsset names in this Skate build.
        roles={'99e879a3':'base','f68fbcc7':'base','d6f7af17':'normal','793db013':'normal','b5ed9695':'mask','b5edaf17':'mask','52e03bf3':'mask'}
        for slot,value in packet.items():
            if isinstance(value,list) and value:
                guid=value[0]['file'];bindings[slot]=guid
                if slot in roles:
                    path=self.texture(guid)
                    if path:paths[roles[slot]]=path
                    else:missing.append({'slot':slot,'guid':guid})
        rel='materials/frostbite/'+digest+'.json';params=[[1,1,1,1],[.7,0,1,0],[1,1,0,0],[0,0,0,0]]
        normal_asset=self.archive.by_guid.get(bindings.get('d6f7af17',bindings.get('793db013','')))
        if normal_asset and '_ny' in normal_asset[0]['name']:params[1][3]=1
        obj={'format':'aura-frostbite-material','version':1,'name':name,'state_key':str(key),'textures':paths,
             'parameters':params,'shader_packet':packet,'texture_bindings':bindings,'unresolved_textures':missing,
             'double_sided':True,'alpha_cutoff':.333 if any(x in name.lower() for x in ['foliage','grass','leaf','decal']) else -1}
        p=self.assets/rel;p.parent.mkdir(parents=True,exist_ok=True);p.write_text(json.dumps(obj,indent=2))
        self.materials[digest]=rel;return rel
    def convert_mesh(self,mesh,collision=False):
        root=mesh[1];rid=root['MeshSetResource']['$resource'];cache=(rid,collision)
        if cache in self.meshes:return self.meshes[cache]
        res=self.archive.resources.get(rid)
        if not res:raise ValueError('Mesh resource missing: '+rid)
        sections=mesh_sections(self.archive,res)
        if not sections:raise ValueError('Mesh contains no triangle sections')
        mats=[] if collision else [self.material(s['state_key'],s['name']) for s in sections]
        rel='meshes/frostbite/'+rid+'.amsh';verts,tris=write_mesh(self.assets/rel,sections)
        self.counts['vertices']+=verts;self.counts['triangles']+=tris
        self.meshes[cache]=(rel,mats);return rel,mats
    def convert_collision(self,doc,model):
        physics=None
        for reference in model.get('Components',[]):
            part=self.archive.resolve(doc,reference)
            if part and part[1].get('$type')=='FBPhysicsComponentData':
                physics=self.archive.resolve(part[0],part[1].get('Asset'));break
        if not physics:raise ValueError('Collision actor has no physics asset')
        rid=physics[1]['Resource']['$resource'];cache=(rid,True)
        if cache in self.meshes:return self.meshes[cache]
        res=self.archive.resources.get(rid)
        if not res:raise ValueError('Physics resource missing')
        sections=physics_sections(self.archive,res);rel='meshes/frostbite/collision_'+rid+'.amsh'
        verts,tris=write_mesh(self.assets/rel,sections)
        self.counts['vertices']+=verts;self.counts['triangles']+=tris
        self.meshes[cache]=(rel,[]);return rel,[]
    def floor_at(self,x,z):
        heights=[]
        for path,transform in self.collision_geometry:
            raw=(self.assets/path).read_bytes();h=struct.unpack_from('<16I',raw);nv,ni=h[4:6];ib=(96+nv*48+15)&~15
            vertices=np.frombuffer(raw,dtype='<f4',count=nv*12,offset=96).reshape(-1,12)[:,:3]
            vertices=vertices@transform[:3,:3].T+transform[:3,3]
            indices=np.frombuffer(raw,dtype='<u4',count=ni,offset=ib).reshape(-1,3)
            a,b,c=vertices[indices[:,0]],vertices[indices[:,1]],vertices[indices[:,2]]
            den=(b[:,2]-c[:,2])*(a[:,0]-c[:,0])+(c[:,0]-b[:,0])*(a[:,2]-c[:,2]);valid=np.abs(den)>1e-8
            alpha=np.zeros(len(indices));beta=alpha.copy()
            alpha[valid]=((b[valid,2]-c[valid,2])*(x-c[valid,0])+(c[valid,0]-b[valid,0])*(z-c[valid,2]))/den[valid]
            beta[valid]=((c[valid,2]-a[valid,2])*(x-c[valid,0])+(a[valid,0]-c[valid,0])*(z-c[valid,2]))/den[valid]
            gamma=1-alpha-beta;inside=valid&(alpha>=0)&(beta>=0)&(gamma>=0)
            yy=alpha*a[:,1]+beta*b[:,1]+gamma*c[:,1];heights.extend(yy[inside].tolist())
        if not heights:raise ValueError('Generated spawn has no native collision floor; choose a spawn explicitly')
        return max(heights)
    def scene(self,map_name):
        places=[];layers=0
        for name,(asset,doc) in self.archive.documents.items():
            if asset.get('root_type')!='LayerData' or '/wpvista/' in name or map_name not in name:continue
            layers+=1
            for index,obj in enumerate(doc['objects']):
                if obj.get('$type')!='ObjectReferenceObjectData' or obj.get('Excluded'):continue
                try:
                    bp=self.archive.resolve(doc,obj.get('Blueprint'))
                    if not bp:raise ValueError('Blueprint unresolved')
                    model=self.archive.resolve(bp[0],bp[1].get('Object'))
                    if not model or model[1].get('$type')!='StaticModelEntityData':continue
                    mesh=self.archive.resolve(model[0],model[1].get('Mesh'))
                    if not mesh:raise ValueError('Mesh asset unresolved')
                    collision=not model[1].get('Visible',True)
                    path,mats=self.convert_collision(model[0],model[1]) if collision else self.convert_mesh(mesh)
                    transform=matrix(obj['BlueprintTransform'])@matrix(model[1]['Transform'])
                    if collision:
                        self.collision_geometry.append((path,transform))
                    # Preserve arbitrary placement matrices in the manifest. The ALVL writer
                    # rejects shear rather than silently changing a source placement.
                    tags={'mesh':path,'source':mesh[1].get('Name',''),'source_level':name,'level_instance':name}
                    tags.update({f'material_{i}':m for i,m in enumerate(mats)})
                    places.append({'kind':'frostbite_collision' if collision else 'frostbite_mesh','name':mesh[1].get('Name','').split('/')[-1],
                                   'matrix':transform.tolist(),'tags':tags,'values':{'collision':1 if collision else 0}})
                except Exception as e:self.errors.append({'stage':'placement','layer':name,'index':index,'error':str(e)})
        if not places:raise ValueError('No map placements were converted')
        # A convenient editor spawn until the game's ECS start-point payload is decoded.
        positions=np.array([np.array(p['matrix'])[:3,3] for p in places]);spawn=np.median(positions,axis=0).tolist();spawn[1]=self.floor_at(spawn[0],spawn[2])+0.5
        title='Skate3 University' if map_name=='dingolevel_reskate_skate3_university' else map_name
        places.insert(0,{'kind':'frostbite_environment','name':title+' outdoor settings','at':[0,0,0],'tags':{},'values':{'stream_load_distance':256,'stream_unload_distance':320,'sun_intensity':2.4}})
        places.insert(0,{'kind':'spawn','name':title+' editor spawn','at':spawn,'tags':{},'values':{}})
        report={'map':map_name,'layers':layers,'placements':len(places),'meshes':len(self.meshes),'materials':len(self.materials),
                'textures':len(self.texture_paths),'counts':self.counts,'errors':self.errors,
                'limitations':['Vista LOD duplicates excluded; highest-detail spatial layers imported.',
                               'Native shader texture bindings retained; shader execution and lighting use Aura approximations.',
                               'Editor spawn is generated; ECS gameplay and grind mechanics are not translated.']}
        return {'format':'aura-frostbite-scene','version':1,'name':title,'places':places},report

def main():
    p=argparse.ArgumentParser();p.add_argument('--export',required=True);p.add_argument('--textures',required=True);p.add_argument('--assets',required=True);p.add_argument('--out',required=True);p.add_argument('--map',default='dingolevel_reskate_skate3_university');a=p.parse_args()
    archive=Archive(a.export);converter=Converter(archive,a.textures,a.assets)
    try:scene,report=converter.scene(a.map)
    finally:converter.finish_textures()
    out=Path(a.out);out.parent.mkdir(parents=True,exist_ok=True);out.write_text(json.dumps(scene,indent=2));out.with_suffix('.report.json').write_text(json.dumps(report,indent=2))
    print(json.dumps({k:v for k,v in report.items() if k not in ['errors','limitations']},indent=2));print('Errors:',len(report['errors']))
if __name__=='__main__':main()
