import React,{useEffect,useState} from 'react';
import {AbsoluteFill,Composition,Easing,Html5Audio,cancelRender,continueRender,delayRender,interpolate,registerRoot,spring,staticFile,useCurrentFrame,useVideoConfig} from 'remotion';
const ink='#142744',blue='#2465E8',mint='#159C78',cream='#FBF4EA';
const clamp=(v:number)=>Math.max(0,Math.min(1,v));
const tween=(f:number,a:number,b:number)=>interpolate(f,[a,b],[0,1],{extrapolateLeft:'clamp',extrapolateRight:'clamp',easing:Easing.inOut(Easing.cubic)});
const pop=(f:number,start:number)=>spring({frame:f-start,fps:30,config:{damping:23,stiffness:95,mass:.8}});
const label=(s:string,x:number,y:number,size=22,color=ink,weight=550,anchor:'start'|'middle'|'end'='start')=><text x={x} y={y} fill={color} fontSize={size} fontWeight={weight} textAnchor={anchor}>{s}</text>;
function Logo({x,y,size=.3}:{x:number;y:number;size?:number}){return <g transform={`translate(${x} ${y}) scale(${size})`} fill={blue}><path d="M22 83Q43 65 40 42Q56 54 61 72Z M66 87Q96 57 88 21Q118 43 125 68Z M126 91Q159 47 148 0Q175 25 180 62Z M39 82 59 79Q53 87 45 87Z M83 86 122 77Q107 94 91 92Z M142 88 180 73Q163 101 131 104Z M0 104Q40 78 87 94Q135 111 180 77Q151 118 91 106Q43 91 0 104Z"/></g>}
function Tick({p=1,size=1}:{p?:number;size?:number}){return <g transform={`scale(${size})`}><path d="M-12 0-3 9 15-11" fill="none" stroke="currentColor" strokeWidth="4" strokeLinecap="round" strokeLinejoin="round" pathLength="1" strokeDasharray="1" strokeDashoffset={1-clamp(p)}/></g>}
function Parcel({x=0,y=0,size=1}:{x?:number;y?:number;size?:number}){return <g transform={`translate(${x} ${y}) scale(${size})`} stroke="#ECF4FF" strokeWidth="1.6" strokeLinejoin="round"><ellipse cx="0" cy="28" rx="25" ry="8" fill="#163777" opacity=".1" stroke="none"/><path d="M-24-13 0-27 26-14 1 0Z" fill="#6D9CFF"/><path d="M-24-13 1 0V29L-24 15Z" fill="#2B69DD"/><path d="M1 0 26-14V14L1 29Z" fill="#1648AC"/><path d="M-11-20 13-7V7" stroke="white" strokeWidth="4" fill="none"/></g>}
function Seal({x=0,y=0,p=1}:{x?:number;y?:number;p?:number}){return <g transform={`translate(${x} ${y})`}><circle r="23" fill="#E5F5EC" stroke={mint} strokeWidth="2"/><g color={mint}><Tick p={p}/></g></g>}
const paths={
 approval:[[625,510],[675,465],[715,405],[800,405]],
 upload:[[450,565],[650,530],[760,625],[960,625]],
 check:[[1370,480],[1220,380],[1000,350],[800,405]],
 permit:[[800,405],[1000,440],[1220,520],[1370,480]],
 download:[[960,625],[1120,615],[1260,535],[1370,480]],
 gateway:[[960,625],[1160,590],[1230,310],[1424,304]],
 sensor:[[960,625],[1120,630],[1210,694],[1375,694]],
};
function cubic(points:number[][],t:number){const [a,b,c,d]=points,q=1-t;return [q**3*a[0]+3*q*q*t*b[0]+3*q*t*t*c[0]+t**3*d[0],q**3*a[1]+3*q*q*t*b[1]+3*q*t*t*c[1]+t**3*d[1]]}
function Transfer({f,type,start,end}:{f:number;type:keyof typeof paths;start:number;end:number}){
 if(f<start||f>end+38)return null;const points=paths[type],p=tween(f,start,end),[x,y]=cubic(points,p),software=['upload','download','gateway','sensor'].includes(type),query=type==='check',color=software?blue:query?'#8298B9':mint,fade=1-tween(f,end+8,end+38);
 return <g opacity={fade}>
  <path d={`M${points[0]}C${points[1]} ${points[2]} ${points[3]}`} fill="none" stroke={software?'#A5C5FF':'#D0E0DB'} strokeWidth={software?17:7} opacity=".35" pathLength="1" strokeDasharray="1" strokeDashoffset={1-p} strokeLinecap="round"/>
  <path d={`M${points[0]}C${points[1]} ${points[2]} ${points[3]}`} fill="none" stroke={color} strokeWidth={software?9:2.8} pathLength="1" strokeDasharray={query?'.012 .018':'1'} strokeDashoffset={1-p} strokeLinecap="round"/>
  {p<1&&(software?<Parcel x={x} y={y} size={.9}/>:query?<g transform={`translate(${x} ${y})`}><circle r="19" fill={cream} stroke={color} strokeWidth="2"/>{label('?',0,7,23,color,600,'middle')}</g>:<Seal x={x} y={y}/>)}
  {f>=end&&<circle cx={points[3][0]} cy={points[3][1]} r={15+33*tween(f,end,end+28)} fill="none" stroke={color} strokeWidth="2" opacity={1-tween(f,end,end+28)}/>}
 </g>;
}
function Waypoint({f,type}:{f:number;type:'sui'|'walrus'}){
 const sui=type==='sui',p=pop(f,sui?240:290);if(p<=0)return null;const x=sui?800:960,y=sui?405:625,confirmed=f>(sui?305:365);
 return <g transform={`translate(${x} ${y+12*(1-p)}) scale(${.95+.05*p})`} opacity={clamp(p)} filter="url(#soft)">
  <path d="M-100 5C-124-24-102-59-65-58C-55-88-12-89 7-66C42-82 75-65 78-39C117-29 118 15 90 30C77 57 36 60 18 48C-10 70-43 57-56 43C-83 51-112 31-100 5Z" fill="#FFF9F1" fillOpacity=".96" stroke={sui?'#8EACD9':'#8FAEE7'} strokeWidth="2"/>
  {label(sui?'Sui':'Walrus',0,-14,29,ink,650,'middle')}
  {label(sui?'Deployment approval':'Software files',0,16,17,'#5F7595',550,'middle')}
  {confirmed&&(sui?<g transform="translate(0 42)" color={mint}><Tick p={tween(f,305,322)} size={.65}/></g>:<Parcel y={48} size={.42}/>)}
 </g>;
}
function Release({f}:{f:number}){
 const p=pop(f,20);return <g opacity={clamp(p)*(1-tween(f,130,145))} transform={`translate(447 ${555+15*(1-p)}) rotate(-4)`}>
  <Parcel size={1.05}/>{label('Release 1.2',0,53,17,ink,650,'middle')}
 </g>;
}
function Wallet({f}:{f:number}){
 const p=pop(f,145),exit=tween(f,247,270);if(p<=0||exit===1)return null;
 return <g transform={`translate(${620+155*exit} ${515-100*exit}) scale(${(.94+.06*p)*(1-.68*exit)})`} opacity={clamp(p)*(1-exit)} filter="url(#soft)">
  <rect x="-125" y="-111" width="250" height="224" rx="18" fill="#FFFBF5" stroke="#C6D4E8"/>
  {label('Wallet approval',-99,-72,21,ink,650)}{label('Factory group · 3 devices',-99,-42,14,'#607493')}
  <path d="M-99-17H99" stroke="#D9E1EB"/>
  <path d="M-75 45Q-60 4-50 38T-20 38Q-6 8 5 40T31 34L76 32" stroke={blue} strokeWidth="3" fill="none" strokeLinecap="round" pathLength="1" strokeDasharray="1" strokeDashoffset={1-tween(f,177,221)}/>
  <g opacity={tween(f,222,240)}><rect x="-99" y="72" width="198" height="27" rx="7" fill="#DFF2E6"/>{label('Deployment approved',0,90,14,'#117A5D',600,'middle')}</g>
 </g>;
}
function Verification({f}:{f:number}){
 const receive=tween(f,450,500),verify=tween(f,500,548);
 if(f<450)return null;
 return <g>
  {verify<.98?<g><rect x="1340" y="492" width="58" height="5" rx="2.5" fill="#355674"/><rect x="1340" y="492" width={58*(f<500?receive:verify)} height="5" rx="2.5" fill={f<500?'#6CB0FF':'#6DE0AF'}/></g>:<g transform="translate(1370 480)" color="#7AE7B8"><Tick p={verify} size={1}/></g>}
  {f>=500&&f<548&&<path d={`M1334 ${448+59*tween(f,500,545)}H1405`} stroke="#7BC9FF" strokeWidth="3" opacity=".6"/>}
  {f>548&&<g opacity={tween(f,548,560)}><path d="M1324 519H1210" stroke={mint} strokeWidth="2"/>{label('Verified',1196,526,24,'#147B5E',650,'end')}</g>}
  {f>560&&<Seal x={1395} y={298} p={tween(f,560,574)}/>}
  {f>570&&<Seal x={1352} y={678} p={tween(f,570,584)}/>}
 </g>;
}
function Film(){
 const f=useCurrentFrame(),{width,height}=useVideoConfig(),portrait=height>width;
 const [handle]=useState(()=>delayRender('Load selected editorial scene and type'));
 useEffect(()=>{const image=new Image();image.src=staticFile('editorial-scene.png');Promise.all([image.decode(),document.fonts.load('600 40px Inter')]).then(()=>continueRender(handle)).catch(cancelRender)},[handle]);
 const frame=portrait?{x:32,y:335,w:836,h:1060}:{x:40,y:215,w:1520,h:560};
 const times=[0,130,240,345,445,550,599];
 const zoom=interpolate(f,times,portrait?[1.36,1.36,1.17,1.07,1.12,1.5,1.5]:[1.15,1.15,1.02,.97,.98,.98,.98],{easing:Easing.inOut(Easing.cubic)});
 const rawX=interpolate(f,times,portrait?[425,425,670,900,1100,1370,1370]:[670,670,790,800,880,1100,1100],{easing:Easing.inOut(Easing.cubic)});
 const cx=Math.max(frame.w/(2*zoom),Math.min(1600-frame.w/(2*zoom),rawX)),cy=Math.max(frame.h/(2*zoom),Math.min(900-frame.h/(2*zoom),portrait?510:490));
 const phase=f<145?0:f<270?1:f<390?2:3;
 return <AbsoluteFill style={{background:cream,fontFamily:'Inter,sans-serif'}}>
  <style>{`@font-face{font-family:Inter;src:url('${staticFile('inter-variable.woff2')}') format('woff2');font-weight:100 900}`}</style>
  <Html5Audio src={staticFile('narration.wav')}/>
  <svg width={width} height={height} viewBox={`0 0 ${width} ${height}`}>
   <defs><clipPath id="scene"><rect x={frame.x} y={frame.y} width={frame.w} height={frame.h} rx="22"/></clipPath><filter id="soft" x="-40%" y="-40%" width="180%" height="200%"><feDropShadow dx="0" dy="6" stdDeviation="8" floodColor="#163B6D" floodOpacity=".1"/></filter></defs>
   <Logo x={portrait?55:74} y={portrait?52:40} size={.35}/>{label('SuiFleet',portrait?130:149,portrait?87:75,30,ink,650)}
   {portrait?<g>{label('Your software.',55,198,57,ink,650)}{label('Every device.',55,267,57,ink,650)}</g>:label('Your software. Every device.',74,165,55,ink,650)}
   <g clipPath="url(#scene)">
    <g transform={`translate(${frame.x+frame.w/2} ${frame.y+frame.h/2}) scale(${zoom}) translate(${-cx} ${-cy})`}>
     <image href={staticFile('editorial-scene.png')} x="0" y="0" width="1600" height="900" preserveAspectRatio="none"/>
     <Release f={f}/>
     {f>=135&&f<173&&<circle cx="420" cy="665" r={10+28*tween(f,135,173)} stroke={blue} strokeWidth="3" fill="none" opacity={1-tween(f,145,173)}/>}
     <Wallet f={f}/>
     <Waypoint f={f} type="sui"/><Waypoint f={f} type="walrus"/>
     <Transfer f={f} type="approval" start={260} end={305}/>
     <Transfer f={f} type="upload" start={308} end={365}/>
     <Transfer f={f} type="check" start={390} end={418}/>
     <Transfer f={f} type="permit" start={420} end={447}/>
     <Transfer f={f} type="download" start={450} end={500}/>
     <Transfer f={f} type="gateway" start={520} end={560}/>
     <Transfer f={f} type="sensor" start={532} end={570}/>
     <Verification f={f}/>
    </g>
   </g>
   <rect x={frame.x} y={frame.y} width={frame.w} height={frame.h} rx="22" fill="none" stroke="#E7DED1"/>
   <path d={`M${portrait?55:74} ${height-73}H${width-(portrait?55:74)}`} stroke="#DBD6CE"/>
   <path d={`M${portrait?55:74} ${height-73}H${(portrait?55:74)+(width-2*(portrait?55:74))*f/599}`} stroke={blue} strokeWidth="2"/>
   {['Prepare','Approve','Deliver','Verify'].map((word,i)=>label(word,(portrait?55:74)+(width-2*(portrait?55:74))*i/3,height-38,15,phase===i?blue:'#6B7890',phase===i?650:500,i===0?'start':i===3?'end':'middle'))}
   {f>=548&&label('Verified. Ready to install.',width/2,portrait?1463:height-96,portrait?24:20,'#157C60',600,'middle')}
  </svg>
 </AbsoluteFill>;
}
const Root=()=> <><Composition id="Landscape" component={Film} width={1600} height={900} fps={30} durationInFrames={600}/><Composition id="Portrait" component={Film} width={900} height={1600} fps={30} durationInFrames={600}/></>;
registerRoot(Root);
