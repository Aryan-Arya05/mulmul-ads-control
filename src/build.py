import re,shutil,json
def parts(path):
    h=open(path).read()
    css=re.search(r'<style>(.*?)</style>',h,re.S).group(1)
    js=re.search(r'<script>(.*)</script>',h,re.S).group(1)
    i=h.index('<div class="wrap">'); j=h.index('<script>')
    return css,h[i:j].rstrip(),js,h
def blocks(css):
    out=[];i=0;n=len(css)
    while i<n:
        j=css.find('{',i)
        if j<0: break
        sel=css[i:j]; depth=1;k=j+1
        while k<n and depth:
            if css[k]=='{':depth+=1
            elif css[k]=='}':depth-=1
            k+=1
        out.append((sel,css[j+1:k-1])); i=k
    return out
def nocom(c): return re.sub(r'/\*.*?\*/','',c,flags=re.S)
def scope(css,sc):
    res=[]
    for sel,body in blocks(nocom(css)):
        s=sel.strip()
        if s.startswith('@media'): res.append(s+'{'+scope(body,sc)+'}')
        elif s.startswith('@'): res.append(s+'{'+body+'}')
        else:
            sels=[]
            for x in s.split(','):
                x=x.strip()
                sels.append(x if (x.startswith(':root') or x in('html','body','*')) else sc+' '+x)
            res.append(','.join(sels)+'{'+body+'}')
    return '\n'.join(res)
dc,db,dj,dh=parts('src/adsdash.html'); sc_,sb,sj,sh=parts('src/scorer.html')
v2=nocom(dc[dc.index('/* ---- theme v2'):])
keep=(':root','@media (prefers-color-scheme','.band','.eyebrow','.bandmeta','.switch','.jump')
bandcss='\n'.join(s.strip()+'{'+b+'}' for s,b in blocks(v2) if s.strip().startswith(keep) or (s.strip().startswith('@media (max-width:760px)') and '.band-in' in b))
strip=lambda s: json.dumps(re.sub(r'<[^>]+>','',s).strip(),ensure_ascii=False)
meta_s=strip(re.search(r'<p class="bandmeta">(.*?)</p>',sh,re.S).group(1)); meta_d=strip(re.search(r'<p class="bandmeta">(.*?)</p>',dh,re.S).group(1))
T=open('src/template.html').read()
page=T.replace('__CSS_SCORER__',scope(sc_,'#view-scorer')).replace('__CSS_DASH__',scope(dc,'#view-dash')).replace('__CSS_BAND__',bandcss).replace('__BODY_DASH__',db).replace('__BODY_SCORER__',sb).replace('__JS_DASH__',dj).replace('__JS_SCORER__',sj).replace('__META_D__',meta_d).replace('__META_S__',meta_s)
open('index.html','w').write(page)
for i,x in enumerate(re.findall(r'<script>(.*?)</script>',page,re.S)): open(f'/tmp/z{i}.js','w').write(x)
print(len(page),page.count('__'),page.count('#view-scorer .tile'),page.count('#view-dash .sechead'))
