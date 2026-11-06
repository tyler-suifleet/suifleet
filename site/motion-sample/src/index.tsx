import React, {useEffect, useState} from 'react';
import {AbsoluteFill, Composition, Easing, Html5Audio, cancelRender, continueRender, delayRender, interpolate, registerRoot, spring, staticFile, useCurrentFrame, useVideoConfig} from 'remotion';

const ink='#13233F',muted='#6C7890',blue='#2563EB',mint='#12A884';
const clamp=(n:number)=>Math.max(0,Math.min(1,n));
const tween=(f:number,a:number,b:number)=>interpolate(f,[a,b],[0,1],{extrapolateLeft:'clamp',extrapolateRight:'clamp',easing:Easing.inOut(Easing.cubic)});
const pop=(f:number,start:number)=>spring({frame:f-start,fps:30,config:{damping:22,stiffness:100,mass:.8}});
const text=(content:string,x:number,y:number,size=24,fill=ink,weight=500,anchor:any='start')=><text x={x} y={y} fontSize={size} fontWeight={weight} fill={fill} textAnchor={anchor}>{content}</text>;
const Tick=({x=0,y=0,p=1,size=1}:{x?:number;y?:number;p?:number;size?:number})=><g transform={`translate(${x} ${y}) scale(${size})`}><path d="M-12 0 -3 9 15-11" fill="none" stroke="currentColor" strokeWidth="4" strokeLinecap="round" strokeLinejoin="round" pathLength="1" strokeDasharray="1" strokeDashoffset={1-clamp(p)}/></g>;
const Logo=({x,y,size=1}:{x:number;y:number;size?:number})=><g transform={`translate(${x} ${y}) scale(${size})`} fill={blue}><path d="M22 83Q43 65 40 42Q56 54 61 72Z M66 87Q96 57 88 21Q118 43 125 68Z M126 91Q159 47 148 0Q175 25 180 62Z M39 82 59 79Q53 87 45 87Z M83 86 122 77Q107 94 91 92Z M142 88 180 73Q163 101 131 104Z M0 104Q40 78 87 94Q135 111 180 77Q151 118 91 106Q43 91 0 104Z"/></g>;
const FileIcon=({x=0,y=0,scale=1}:{x?:number;y?:number;scale?:number})=><g transform={`translate(${x} ${y}) scale(${scale})`}><rect x="-27" y="-34" width="54" height="68" rx="10" fill="url(#file)"/><path d="M10-34V-18H27" fill="#A9CBFF"/><path d="M-13-7H11 M-13 4H15 M-13 15H6" stroke="white" strokeWidth="3" strokeLinecap="round"/></g>;
function Console({f}:{f:number}){
 const approved=f>240,p=pop(f,25);
 return <g transform="translate(-390 -120)">
  <image href={staticFile('workstation.png')} x="-300" y="-230" width="600" height="460" preserveAspectRatio="xMidYMid meet"/>
  <g opacity={clamp(p)} transform={`translate(0 ${14*(1-p)})`} filter="url(#smallShadow)">
   <rect x="-155" y="-175" width="310" height="100" rx="18" fill="white" fillOpacity=".98" stroke="#E0E9F6"/>
   <FileIcon x={-118} y={-130} scale={.62}/>
   {text('Gateway software',-88,-142,19,ink,620)}{text('Release 1.2',-88,-116,15,muted)}
   <circle cx="108" cy="-125" r="15" fill={approved?'#E1F6ED':'#EBF1FE'}/>
   {approved?<g color={mint}><Tick x={108} y={-125} size={.48}/></g>:text('↑',108,-119,19,blue,600,'middle')}
  </g>
 </g>;
}
function Cursor({f}:{f:number}){
 if(f<112||f>172)return null;const p=tween(f,112,140),x=interpolate(p,[0,1],[-265,-231]),y=interpolate(p,[0,1],[125,-203]),fade=tween(f,112,122)*(1-tween(f,158,172)),click=tween(f,140,160);
 return <g opacity={fade}>
  {f>=140&&<circle cx={x} cy={y} r={12+27*click} fill="none" stroke={blue} strokeWidth="2" opacity={1-click}/>}
  <g transform={`translate(${x} ${y}) scale(${1-.1*Math.sin(Math.PI*click)})`} filter="url(#smallShadow)"><path d="M0 0 0 30 8 23 15 37 21 34 14 20 25 19Z" fill={ink} stroke="white" strokeWidth="2.5" strokeLinejoin="round"/></g>
 </g>;
}
function Approval({f}:{f:number}){
 const enter=pop(f,145),exit=tween(f,250,275),opacity=clamp(enter)*(1-exit);if(opacity<=0)return null;
 return <g transform={`translate(${-150+15*(1-enter)+180*exit} ${-100-12*enter}) scale(${(.94+.06*enter)*(1-.7*exit)})`} opacity={opacity} filter="url(#cardShadow)">
  <rect x="-125" y="-133" width="310" height="290" rx="23" fill="white" stroke="#E0E8F5"/>
  <circle cx="-83" cy="-89" r="20" fill="#EDF3FF"/><path d="M-95-92H-72V-75H-95Z M-91-92V-99H-76V-92" fill="none" stroke={blue} strokeWidth="2.5" strokeLinejoin="round"/>
  {text('Your approval',-50,-91,15,muted)}{text('Approve deployment',-94,-42,23,ink,620)}
  {text('Gateway software · 1.2',-94,-9,16,muted)}
  <path d="M-94 18H152" stroke="#E9EEF6"/>
  <path d="M-75 65Q-60 18-50 55T-20 55Q-5 22 5 55T30 48L75 46" fill="none" stroke={blue} strokeWidth="3.2" strokeLinecap="round" pathLength="1" strokeDasharray="1" strokeDashoffset={1-tween(f,185,227)}/>
  <rect x="-94" y="100" width="246" height="35" rx="9" fill="#E4F6EF" opacity={tween(f,225,245)}/>
  <g color={mint} opacity={tween(f,225,245)}><Tick x={-71} y={117} p={tween(f,225,245)} size={.55}/>{text('Deployment approved',-49,122,14,'#0A7A60',600)}</g>
 </g>;
}
function SuiRecord({f}:{f:number}){
 const p=pop(f,255);if(p<=0)return null;
 return <g transform={`translate(420 ${-250+24*(1-p)})`} opacity={clamp(p)} filter="url(#shadow)">
  <rect x="-174" y="-92" width="348" height="184" rx="24" fill="white"/>
  <rect x="-147" y="-65" width="44" height="44" rx="13" fill="#EAF1FF"/>
  <path d="M-135-53H-119V-31H-135Z M-131-45H-123 M-131-39H-123" fill="none" stroke={blue} strokeWidth="2"/>
  {text('Sui',-87,-47,24,ink,650)}{text('Approved release record',-87,-22,14,muted)}
  <path d="M-145 0H145" stroke="#E9EEF6"/>{text('Gateway software',-144,33,17,ink,550)}{text('1.2',145,33,18,blue,650,'end')}
  <g color={mint} opacity={tween(f,320,337)}><Tick x={-135} y={65} size={.5} p={tween(f,320,337)}/>{text('Approval verified',-115,70,14,'#12866A',550)}</g>
 </g>;
}
function Storage({f}:{f:number}){
 const p=pop(f,300);if(p<=0)return null;
 return <g transform={`translate(420 ${160+25*(1-p)})`} opacity={clamp(p)}>
  <image href={staticFile('storage.png')} x="-200" y="-170" width="400" height="330" preserveAspectRatio="xMidYMid meet"/>
  <g opacity={tween(f,382,400)} transform="translate(0 -84)"><FileIcon scale={.7}/></g>
  {text('Walrus',0,182,25,ink,650,'middle')}{text('Software files',0,207,16,muted,450,'middle')}
 </g>;
}
function Gateway({f}:{f:number}){
 const p=pop(f,350);if(p<=0)return null;const ready=tween(f,505,548),load=tween(f,450,495);
 return <g transform={`translate(-390 ${240+35*(1-p)})`} opacity={clamp(p)}>
  <image href={staticFile('gateway.png')} x="-210" y="-215" width="420" height="420" preserveAspectRatio="xMidYMid meet"/>
  <g color="#60F2B9" opacity={ready}><Tick x={-115} y={48} p={ready} size={.7}/></g>
  {f>=500&&f<548&&<rect x="-137" y={29+29*tween(f,500,542)} width="52" height="4" rx="2" fill="url(#scan)" opacity=".7"/>}
  <g opacity={tween(f,450,465)} transform={`translate(115 ${-55+12*(1-ready)})`} filter="url(#smallShadow)">
   <rect x="-10" y="-35" width="210" height="116" rx="17" fill="white"/>
   {text('Release 1.2',10,-5,18,ink,650)}
   {text(f<500?'Receiving files':ready<.98?'Verifying integrity':'Ready to install',10,24,14,ready>.98?'#118468':muted,500)}
   <rect x="10" y="47" width="168" height="5" rx="2.5" fill="#E4ECF7"/>
   <rect x="10" y="47" width={168*(f<500?load:ready)} height="5" rx="2.5" fill={ready>.98?mint:blue}/>
  </g>
 </g>;
}
const flowPaths={approval:[[30,-100],[140,-180],[240,-300],[420,-250]],files:[[-280,-210],[-60,-70],[180,160],[420,160]],check:[[-380,285],[-160,100],[120,-240],[420,-250]],response:[[420,-250],[160,-340],[-350,-90],[-380,285]],download:[[420,160],[220,285],[-150,380],[-380,285]]};
function bezier(points:number[][],t:number){const [a,b,c,d]=points,q=1-t;return [q*q*q*a[0]+3*q*q*t*b[0]+3*q*t*t*c[0]+t*t*t*d[0],q*q*q*a[1]+3*q*q*t*b[1]+3*q*t*t*c[1]+t*t*t*d[1]]}
function Flow({f,type,start,end}:{f:number;type:keyof typeof flowPaths;start:number;end:number}){
 if(f<start||f>end+20)return null;const p=tween(f,start,end),pts=flowPaths[type],pos=bezier(pts,p),fade=1-tween(f,end,end+20),arrival=clamp((f-end)/20);const tangent=[3*(1-p)**2*(pts[1][0]-pts[0][0])+6*(1-p)*p*(pts[2][0]-pts[1][0])+3*p*p*(pts[3][0]-pts[2][0]),3*(1-p)**2*(pts[1][1]-pts[0][1])+6*(1-p)*p*(pts[2][1]-pts[1][1])+3*p*p*(pts[3][1]-pts[2][1])];const bank=Math.max(-16,Math.min(16,Math.atan2(tangent[1],tangent[0])*180/Math.PI*.25));
 return <g opacity={fade}>
  {f>=end&&<circle cx={pts[3][0]} cy={pts[3][1]} r={15+40*arrival} fill="none" stroke={mint} strokeWidth="2" opacity={.45*(1-arrival)}/>}
  <path d={`M${pts[0]}C${pts[1]} ${pts[2]} ${pts[3]}`} fill="none" stroke={type==='check'?'#AFBDD3':'#B9CEF1'} strokeWidth="2" strokeDasharray="5 8" pathLength="1" opacity=".6"/>
  {p<1&&<g transform={`translate(${pos[0]} ${pos[1]}) rotate(${bank})`} filter="url(#smallShadow)">
   <rect x="-47" y="-34" width="94" height="68" rx="14" fill="white" stroke="#DCE6F5"/>
   {type==='check'?<g>{text('Check',0,6,17,blue,650,'middle')}</g>:<g><FileIcon x={-21} scale={.55}/>{text(type==='approval'?'OK':'1.2',13,7,17,type==='approval'?mint:blue,650,'middle')}</g>}
  </g>}
 </g>;
}
function Film(){
 const f=useCurrentFrame(),{width,height}=useVideoConfig(),portrait=height>width;
 const [handle]=useState(()=>delayRender('Load self-hosted typography and artwork'));
 useEffect(()=>{Promise.all([document.fonts.load('600 40px Inter'),...['workstation','gateway','storage'].map(name=>{const image=new Image();image.src=staticFile(name+'.png');return image.decode()})]).then(()=>continueRender(handle)).catch(cancelRender)},[handle]);
 const approvalFocus=tween(f,135,190),reveal=tween(f,215,340),focus=tween(f,448,523);
 const approvalX=interpolate(approvalFocus,[0,1],[-390,portrait?-295:-310]);
 const focusX=interpolate(reveal,[0,1],[approvalX,0])+(portrait?-330:-390)*focus;
 const focusY=interpolate(reveal,[0,1],[-120,0])+240*focus;
 const approvalZoom=interpolate(approvalFocus,[0,1],[portrait?1.25:1.22,portrait?1.06:1.16]);
 const z=interpolate(reveal,[0,1],[approvalZoom,portrait?.53:.76])+(portrait?.92:.79)*focus;
 const phase=f<150?0:f<270?1:f<390?2:f<550?3:4;
 const titles=[['Your next release.'],['Approved by you.'],['Ready for delivery.'],['Your device takes over.'],['Verified.','Ready to install.']];
 const titleIn=pop(f,[0,150,270,390,550][phase]);
 return <AbsoluteFill style={{background:'#F6F8FC',fontFamily:'Inter, sans-serif'}}>
  <style>{`@font-face{font-family:Inter;src:url('${staticFile('inter-variable.woff2')}') format('woff2');font-weight:100 900}`}</style>
  <Html5Audio src={staticFile('narration.wav')}/>
  <svg width={width} height={height} viewBox={`0 0 ${width} ${height}`}>
   <defs>
    <radialGradient id="ambient"><stop stopColor="#DCEAFF" stopOpacity=".9"/><stop offset="1" stopColor="#F6F8FC" stopOpacity="0"/></radialGradient>
    <linearGradient id="scan"><stop stopColor="#75BAFF" stopOpacity="0"/><stop offset=".5" stopColor="#75BAFF"/><stop offset="1" stopColor="#75BAFF" stopOpacity="0"/></linearGradient>
    <linearGradient id="file" x2="1" y2="1"><stop stopColor="#66AAFF"/><stop offset="1" stopColor="#245DE0"/></linearGradient>
    <linearGradient id="storage" x2="0" y2="1"><stop stopColor="#C7DDFC"/><stop offset="1" stopColor="#94BAEE"/></linearGradient>
    <linearGradient id="device" x2="1" y2="1"><stop stopColor="#425975"/><stop offset=".55" stopColor="#2B425F"/><stop offset="1" stopColor="#1A2E4B"/></linearGradient>
    <filter id="shadow" x="-40%" y="-40%" width="180%" height="200%"><feDropShadow dx="0" dy="18" stdDeviation="25" floodColor="#253D70" floodOpacity=".12"/></filter>
    <filter id="cardShadow" x="-50%" y="-50%" width="200%" height="220%"><feDropShadow dx="0" dy="14" stdDeviation="22" floodColor="#19385E" floodOpacity=".2"/></filter>
    <filter id="smallShadow" x="-60%" y="-70%" width="220%" height="250%"><feDropShadow dx="0" dy="8" stdDeviation="10" floodColor="#28456E" floodOpacity=".15"/></filter>
    <filter id="deviceShadow" x="-40%" y="-30%" width="190%" height="180%"><feDropShadow dx="10" dy="20" stdDeviation="20" floodColor="#1A3454" floodOpacity=".18"/></filter>
    <filter id="glow"><feGaussianBlur stdDeviation="3"/><feMerge><feMergeNode/><feMergeNode in="SourceGraphic"/></feMerge></filter>
   </defs>
   <ellipse cx={width*.57} cy={height*.58} rx={width*.7} ry={height*.5} fill="url(#ambient)"/>
   <Logo x={portrait?58:76} y={portrait?67:54} size={portrait?.36:.3}/>
   {text('SuiFleet',portrait?139:145,portrait?101:82,30,ink,650)}
   {text('SOFTWARE, IN SYNC.',width-(portrait?60:78),portrait?98:80,portrait?14:13,muted,600,'end')}
   <g transform={`translate(${portrait?60:78} ${(portrait?205:176)+16*(1-titleIn)})`} opacity={clamp(titleIn)}>
    {titles[phase].map((line,i)=>text(line,0,i*(portrait?72:68),portrait?58:56,ink,600))}
   </g>
   <g transform={`translate(${width/2} ${height*(portrait?.54:.55)}) scale(${z}) translate(${-focusX} ${-focusY})`}>
    <g opacity={1-tween(f,400,445)}><Console f={f}/></g><g opacity={1-tween(f,450,490)}><SuiRecord f={f}/></g><g opacity={1-tween(f,500,530)}><Storage f={f}/></g><Gateway f={f}/>
    <Cursor f={f}/><Approval f={f}/>
    <Flow f={f} type="approval" start={274} end={319}/><Flow f={f} type="files" start={330} end={382}/>
    <Flow f={f} type="check" start={390} end={420}/><Flow f={f} type="response" start={422} end={447}/>
    <Flow f={f} type="download" start={450} end={495}/>
   </g>
   <path d={`M${portrait?60:78} ${height-72}H${width-(portrait?60:78)}`} stroke="#DEE5F0"/>
   <path d={`M${portrait?60:78} ${height-72}H${(portrait?60:78)+(width-2*(portrait?60:78))*f/599}`} stroke={blue} strokeWidth="2"/>
   {text('Prepare',portrait?60:78,height-36,14,phase===0?blue:muted,500)}
   {text('Approve',width*.36,height-36,14,phase===1?blue:muted,500,'middle')}
   {text('Deliver',width*.63,height-36,14,phase===2?blue:muted,500,'middle')}
   {text('Verify',width-(portrait?60:78),height-36,14,phase>=3?blue:muted,500,'end')}
  </svg>
 </AbsoluteFill>;
}
const Root=()=> <><Composition id="Landscape" component={Film} width={1600} height={900} fps={30} durationInFrames={600}/><Composition id="Portrait" component={Film} width={900} height={1600} fps={30} durationInFrames={600}/></>;
registerRoot(Root);
