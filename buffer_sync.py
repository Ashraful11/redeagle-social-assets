#!/usr/bin/env python3
"""RedEagle Buffer sync: keep the next N posts scheduled per channel (free plan: 10).
Env: BUFFER_API_KEY. Args: --manifest <url|path> [--keep 10] [--dry-run] [--status] [--platforms a,b] (default: every *_channel_id in config)
Per-platform publish time: post[platform].publish_utc if set, else post.publish_utc.
Manifest top-level config: org_id(optional), instagram_channel_id, pinterest_channel_id, pinterest_board_id, pinterest_link,
 and asset_urls {file name: public url}."""
import os,sys,re,json,time,argparse,datetime,urllib.request,urllib.error
API='https://api.buffer.com'
def gql(q,v=None):
    key=os.environ['BUFFER_API_KEY']
    req=urllib.request.Request(API,json.dumps({'query':q,'variables':v or {}}).encode(),{'Authorization':key,'Content-Type':'application/json','User-Agent':'redeagle-sync'})
    for a in range(4):
        try:
            r=json.load(urllib.request.urlopen(req,timeout=60))
            if 'errors' in r: raise RuntimeError(json.dumps(r['errors'])[:600])
            return r['data']
        except urllib.error.HTTPError as e:
            if e.code==429: time.sleep(int(e.headers.get('Retry-After','30'))+1); continue
            raise RuntimeError('HTTP %s %s'%(e.code,e.read()[:400]))
    raise RuntimeError('rate limited')
def load(src):
    if src.startswith('http'): return json.load(urllib.request.urlopen(src,timeout=60))
    return json.load(open(src))
def org_and_channels():
    o=gql('query{account{organizations{id name}}}')['account']['organizations'][0]['id']
    ch=gql('query($i:ChannelsInput!){channels(input:$i){id name service}}',{'i':{'organizationId':o}})['channels']
    return o,ch
def posts(org,status,channels):
    out=[];after=None
    while True:
        d=gql('query($i:PostsInput!,$a:String){posts(first:50,after:$a,input:$i){edges{node{id text dueAt channelId status}}pageInfo{hasNextPage endCursor}}}',
              {'i':{'organizationId':org,'filter':{'status':status,'channelIds':channels}},'a':after})['posts']
        out+=[e['node'] for e in d['edges']]
        if not d['pageInfo']['hasNextPage']: return out
        after=d['pageInfo']['endCursor']
def preflight(url,kind):
    try:
        r=urllib.request.urlopen(urllib.request.Request(url,headers={'Range':'bytes=0-1023'}),timeout=40)
        ct=r.headers.get('Content-Type','')
        return ct.startswith(kind+'/') or (kind=='video' and ct=='application/octet-stream' and url.endswith('.mp4')),ct
    except Exception as e: return False,str(e)[:80]
def assets_for(p,plat,urls):
    a=[]
    for n in p[plat]['assets']:
        u=urls[n]; a.append({'video':{'url':u}} if n.endswith('.mp4') else {'image':{'url':u}})
    return a
def cap_tags(t,n=5):
    h=re.findall(r'#\w+',t)
    if len(h)<=n: return t
    return re.sub(r'(\s*#\w+)+\s*$','',t).rstrip()+'\n\n'+' '.join(h[:n])
def due(p,plat):
    return (p.get(plat) or {}).get('publish_utc') or p['publish_utc']
def build(p,plat,cfg,urls):
    if plat=='instagram':
        i=p['instagram']; md={'instagram':{'type':'post' if i['type']=='carousel' else i['type'],'shouldShareToFeed':True}}
        if i.get('first_comment') and cfg.get('use_first_comment'): md['instagram']['firstComment']=i['first_comment']
        text=cap_tags(i['text'])   # Instagram accepts max 5 hashtags
    elif plat=='facebook':
        i=p['facebook']; md={'facebook':{'type':i['type']}}
        text=i['text']
    else:
        i=p['pinterest']; md={'pinterest':{'boardServiceId':cfg['pinterest_board_id'],'title':i['title'],'url':cfg.get('pinterest_link') or i['link']}}
        text=i['description']
    cid=cfg[plat+'_channel_id']
    return text,{'text':text,'channelId':cid,'schedulingType':'automatic','mode':'customScheduled','dueAt':due(p,plat),'assets':assets_for(p,plat,urls),'metadata':md}
M='mutation($i:CreatePostInput!){createPost(input:$i){... on PostActionSuccess{post{id dueAt assets{id}}} ... on MutationError{message}}}'
def main():
    ap=argparse.ArgumentParser();ap.add_argument('--manifest',required=True);ap.add_argument('--keep',type=int,default=10)
    ap.add_argument('--dry-run',action='store_true');ap.add_argument('--status',action='store_true');ap.add_argument('--platforms',default='');ap.add_argument('--only',default='');a=ap.parse_args()
    m=load(a.manifest);cfg=m['config'];urls=m['asset_urls'];now=datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None).strftime('%Y-%m-%dT%H:%M:%S')
    org=cfg.get('org_id') or org_and_channels()[0]
    rep={'created':[],'failed':[],'skipped_past':[],'errors_seen':[],'sent_recent':0}
    plats=a.platforms.split(',') if a.platforms else [k[:-11] for k in cfg if k.endswith('_channel_id')]
    for plat in plats:
        cid=cfg[plat+'_channel_id']
        sched=posts(org,['scheduled'],[cid]); have={p['text'] for p in sched}; n=len(sched)
        errs=posts(org,['error'],[cid]); rep['errors_seen']+=[{'platform':plat,'id':e['id'],'due':e['dueAt'],'text':e['text'][:60]} for e in errs]
        rep['sent_recent']+=len(posts(org,['sent'],[cid])[:20])
        print(plat,'scheduled now:',n)
        if a.status: continue
        for p in sorted([p for p in m['posts'] if plat in p],key=lambda p:due(p,plat)):
            if n>=a.keep: break
            if a.only and str(p['seq']) not in a.only.split(','): continue
            if due(p,plat)[:19]<=now: continue            # slot already passed
            text,inp=build(p,plat,cfg,urls)
            if text in have: continue
            bad=[(nm,ct) for nm in p[plat]['assets'] for ok,ct in [preflight(urls[nm],'video' if nm.endswith('.mp4') else 'image')] if not ok]
            if bad: rep['failed'].append({'platform':plat,'post':p['post_id'],'why':'asset not publicly reachable: %s'%bad}); continue
            if a.dry_run: print('DRY',plat,p['seq'],p['post_id'],p['publish_utc']); n+=1; continue
            r=gql(M,{'i':inp})['createPost']
            if r.get('post') and len(r['post'].get('assets') or [])!=len(inp['assets']): rep['failed'].append({'platform':plat,'post':p['post_id'],'why':'asset count mismatch: sent %d got %d (post %s needs fixing)'%(len(inp['assets']),len(r['post'].get('assets') or []),r['post']['id'])})
            if r.get('post'): n+=1; have.add(text); rep['created'].append({'platform':plat,'seq':p['seq'],'post':p['post_id'],'due':r['post']['dueAt']})
            else: rep['failed'].append({'platform':plat,'post':p['post_id'],'why':r.get('message')}); 
            if r.get('message') and 'limit' in r['message'].lower(): break
        print(plat,'scheduled after:',n)
    print(json.dumps(rep,indent=1))
if __name__=='__main__': main()
