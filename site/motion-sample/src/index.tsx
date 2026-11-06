import React, {useEffect, useState} from 'react';
import {AbsoluteFill, Composition, Easing, Html5Audio, continueRender, delayRender, interpolate, registerRoot, spring, staticFile, useCurrentFrame, useVideoConfig} from 'remotion';

const ink='#13233F',muted='#6C7890',blue='#2563EB',mint='#12A884';
const clamp=(n:number)=>Math.max(0,Math.min(1,n));
const tween=(f:number,a:number,b:number)=>interpolate(f,[a,b],[0,1],{extrapolateLeft:'clamp',extrapolateRight:'clamp',easing:Easing.inOut(Easing.cubic)});
const pop=(f:number,start:number)=>spring({frame:f-start,fps:30,config:{damping:22,stiffness:100,mass:.8}});
const text=(content:string,x:number,y:number,size=24,fill=ink,weight=500,anchor:any='start')=><text x={x} y={y} fontSize={size} fontWeight={weight} fill={fill} textAnchor={anchor}>{content}</text>;
const Tick=({x=0,y=0,p=1,size=1}:{x?:number;y?:number;p?:number;size?:number})=><g transform={`translate(${x} ${y}) scale(${size})`}><path d="M-12 0 -3 9 15-11" fill="none" stroke="currentColor" strokeWidth="4" strokeLinecap="round" strokeLinejoin="round" pathLength="1" strokeDasharray="1" strokeDashoffset={1-clamp(p)}/></g>;
const Logo=({x,y,size=1}:{x:number;y:number;size?:number})=><g transform={`translate(${x} ${y}) scale(${size})`} fill={blue}><path d="M22 83Q43 65 40 42Q56 54 61 72Z M66 87Q96 57 88 21Q118 43 125 68Z M126 91Q159 47 148 0Q175 25 180 62Z M39 82 59 79Q53 87 45 87Z M83 86 122 77Q107 94 91 92Z M142 88 180 73Q163 101 131 104Z M0 104Q40 78 87 94Q135 111 180 77Q151 118 91 106Q43 91 0 104Z"/></g>;
const FileIcon=({x=0,y=0,scale=1}:{x?:number;y?:number;scale?:number})=><g transform={`translate(${x} ${y}) scale(${scale})`}><rect x="-27" y="-34" width="54" height="68" rx="10" fill="url(#file)"/><path d="M10-34V-18H27" fill="#A9CBFF"/><path d="M-13-7H11 M-13 4H15 M-13 15H6" stroke="white" strokeWidth="3" strokeLinecap="round"/></g>;
function Console({f}:{f:number}){
 const approved=f>240;const press=tween(f,160,175)*(1-tween(f,185,200));
 return <g transform="translate(-390 -120)" filter="url(#shadow)">
  <rect x="-270" y="-185" width="540" height="370" rx="23" fill="white"/>
  <path d="M-247-185H-155V185H-247Q-270 185-270 162V-162Q-270-185-247-185" fill={ink}/>
  <Logo x={-250} y={-151} size={.36}/>
  {[0,1,2,3].map(i=><g key={i} opacity={i===1?1:.4}><rect x="-251" y={-69+i*46} width="76" height="30" rx="7" fill={i===1?'#304667':'transparent'}/><rect x="-237" y={-58+i*46} width="12" height="8" rx="2" fill="#A6B8D5"/><rect x="-215" y={-58+i*46} width="27" height="7" rx="3" fill="#A6B8D5"/></g>)}
  {text('Software releases',-122,-131,25,ink,650)}{text('Your fleet. Your software.',-122,-101,15,muted)}
  <g opacity={clamp(pop(f,25))} transform={`translate(0 ${14*(1-pop(f,25))})`}><rect x="-123" y="-70" width="357" height="146" rx="15" fill="#F7F9FD" stroke="#E6ECF5"/>
  <FileIcon x={-78} y={-16} scale={.7}/>{text('Gateway software',-41,-26,20,ink,620)}{text('Version 1.2',-41,2,15,muted)}
  <rect x="-100" y="42" width="116" height="22" rx="11" fill="#E5F6F0"/>{text(approved?'Published':'Ready to publish',-87,57,12,'#137963',600)}</g>
  <rect x="-123" y="95" width="164" height="46" rx="10" fill={approved?mint:blue} transform={`translate(0 ${press*3})`}/>
  {text(approved?'Approved':'Approve deployment',-41,124+press*3,14,'white',600,'middle')}
  <circle cx="217" cy="-136" r="15" fill="#EDF2FA"/>
  {text('Release library',-122,168,12,muted)}
 </g>;
}
function Approval({f}:{f:number}){
 const enter=pop(f,145),exit=tween(f,256,281),opacity=clamp(enter)*(1-exit);if(opacity<=0)return null;
 return <g transform={`translate(${-150+15*(1-enter)} ${-100-12*enter}) scale(${.94+.06*enter})`} opacity={opacity} filter="url(#cardShadow)">
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
  <g color={mint} opacity={tween(f,290,315)}><Tick x={-135} y={65} size={.5} p={tween(f,290,315)}/>{text('Approval verified',-115,70,14,'#12866A',550)}</g>
 </g>;
}
function Storage({f}:{f:number}){
 const p=pop(f,290);if(p<=0)return null;
 return <g transform={`translate(420 ${160+25*(1-p)})`} opacity={clamp(p)}>
  <ellipse cx="0" cy="91" rx="160" ry="23" fill="#16397A" opacity=".09"/>
  {[2,1,0].map(i=><g key={i} transform={`translate(0 ${i*31})`} filter={i===0?'url(#smallShadow)':undefined}>
   <path d="M-154-46Q0-98 154-46V-9Q0 42-154-9Z" fill={i===0?'url(#storage)':'#CBDDF7'} stroke="#BAD1EF" strokeWidth="1.2"/>
   <ellipse cx="0" cy="-46" rx="154" ry="51" fill={i===0?'#EAF2FF':'#DFEBFA'} stroke="#BFD3F0"/>
   <circle cx="-115" cy="-15" r="3.5" fill={i===0?mint:'#8EAED7'}/>
  </g>)}
  <g opacity={tween(f,330,365)} transform={`translate(0 ${-75-10*tween(f,330,365)})`}><FileIcon scale={1.05}/></g>
  {text('Walrus',0,154,26,ink,650,'middle')}{text('Software files',0,181,17,muted,450,'middle')}
 </g>;
}
function Gateway({f}:{f:number}){
 const p=pop(f,350);if(p<=0)return null;const ready=tween(f,545,582),load=tween(f,475,540);
 return <g transform={`translate(-390 ${240+35*(1-p)})`} opacity={clamp(p)}>
  <ellipse cx="10" cy="153" rx="167" ry="24" fill="#19355C" opacity=".12"/>
  <g filter="url(#deviceShadow)">
   <path d="M-142-124-91-157 157-139 105-104Z" fill="#51647E"/>
   <path d="M105-104 157-139V100L105 137Z" fill="#172B47"/>
   <rect x="-142" y="-124" width="247" height="261" rx="16" fill="url(#device)"/>
   <rect x="-121" y="-103" width="204" height="154" rx="11" fill="#0C1D34" stroke="#58718E"/>
   <path d="M-113-95H74" stroke="#8CA5C2" strokeOpacity=".15"/>
   {text('EDGE CLIENT',-100,-69,12,'#98ACC4',600)}
   <g opacity={1-ready}>{text(f<460?'Ready':f<540?'Receiving':'Verifying',-99,-31,23,'#DDEBFF',550)}</g>
   <g opacity={ready} color="#55E5B1"><Tick x={-86} y={-33} p={ready} size={.9}/>{text('Verified',-61,-24,24,'#80EFC5',550)}</g>
   <rect x="-99" y="4" width="159" height="5" rx="2.5" fill="#31465F"/>
   <rect x="-99" y="4" width={159*load} height="5" rx="2.5" fill={ready>.5?'#55DCAE':'#5EA9FF'}/>
   {[0,1,2,3,4,5].map(i=><rect key={i} x={-111+i*25} y="77" width="14" height="29" rx="3" fill="#142942" stroke="#496079"/>)}
   <circle cx="62" cy="88" r="6" fill={ready>.8?'#5BE5B5':'#60ADFC'} filter="url(#glow)"/>
  </g>
  {text('Your device',-12,190,24,ink,600,'middle')}
  <g opacity={ready} transform={`translate(145 ${-40+12*(1-ready)})`} filter="url(#smallShadow)">
   <rect x="-12" y="-35" width="190" height="91" rx="17" fill="white"/>
   {text('Release 1.2',9,-1,17,ink,650)}{text('Ready to install',9,27,14,'#118468',500)}
  </g>
 </g>;
}
const flowPaths={approval:[[-180,-120],[80,-300],[240,-300],[420,-250]],files:[[-180,-50],[50,90],[180,160],[420,160]],check:[[-330,180],[-160,100],[120,-240],[420,-250]],response:[[420,-250],[160,-340],[-350,-90],[-390,240]],download:[[420,160],[220,285],[-150,380],[-390,240]]};
function bezier(points:number[][],t:number){const [a,b,c,d]=points,q=1-t;return [q*q*q*a[0]+3*q*q*t*b[0]+3*q*t*t*c[0]+t*t*t*d[0],q*q*q*a[1]+3*q*q*t*b[1]+3*q*t*t*c[1]+t*t*t*d[1]]}
function Flow({f,type,start,end}:{f:number;type:keyof typeof flowPaths;start:number;end:number}){
 if(f<start||f>end+20)return null;const p=tween(f,start,end),pts=flowPaths[type],pos=bezier(pts,p),fade=1-tween(f,end,end+20);
 return <g opacity={fade}>
  <path d={`M${pts[0]}C${pts[1]} ${pts[2]} ${pts[3]}`} fill="none" stroke={type==='check'?'#AFBDD3':'#B9CEF1'} strokeWidth="2" strokeDasharray="5 8" pathLength="1" opacity=".6"/>
  {p<1&&<g transform={`translate(${pos[0]} ${pos[1]}) rotate(${-7+14*p})`} filter="url(#smallShadow)">
   <rect x="-47" y="-34" width="94" height="68" rx="14" fill="white" stroke="#DCE6F5"/>
   {type==='check'?<g>{text('Check',0,6,17,blue,650,'middle')}</g>:<g><FileIcon x={-21} scale={.55}/>{text(type==='approval'?'OK':'1.2',13,7,17,type==='approval'?mint:blue,650,'middle')}</g>}
  </g>}
 </g>;
}
function Film(){
 const f=useCurrentFrame(),{width,height}=useVideoConfig(),portrait=height>width;
 const [handle]=useState(()=>delayRender('Load self-hosted Inter'));
 useEffect(()=>{document.fonts.load('600 40px Inter').then(()=>continueRender(handle))},[handle]);
 const reveal=tween(f,125,270),focus=tween(f,385,550);
 const focusX=interpolate(reveal,[0,1],[-390,0])+(portrait?-330:-390)*focus;
 const focusY=interpolate(reveal,[0,1],[-120,0])+240*focus;
 const z=(portrait?interpolate(reveal,[0,1],[1.45,.53]):interpolate(reveal,[0,1],[1.32,.76]))+(portrait?.92:.79)*focus;
 const phase=f<150?0:f<270?1:f<420?2:f<558?3:4;
 const titles=[['Your next release.'],['Approved by you.'],['Ready for delivery.'],['Your device takes over.'],['Verified.','Ready to install.']];
 const titleIn=pop(f,[0,150,270,420,558][phase]);
 return <AbsoluteFill style={{background:'#F6F8FC',fontFamily:'Inter, sans-serif'}}>
  <style>{`@font-face{font-family:Inter;src:url('${staticFile('inter-variable.woff2')}') format('woff2');font-weight:100 900}`}</style>
  <Html5Audio src={staticFile('narration.wav')}/>
  <svg width={width} height={height} viewBox={`0 0 ${width} ${height}`}>
   <defs>
    <radialGradient id="ambient"><stop stopColor="#DCEAFF" stopOpacity=".9"/><stop offset="1" stopColor="#F6F8FC" stopOpacity="0"/></radialGradient>
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
    <g opacity={1-tween(f,390,475)}><Console f={f}/></g><g opacity={1-tween(f,460,510)}><SuiRecord f={f}/></g><g opacity={1-tween(f,540,580)}><Storage f={f}/></g><Gateway f={f}/>
    <Approval f={f}/>
    <Flow f={f} type="approval" start={260} end={302}/><Flow f={f} type="files" start={304} end={363}/>
    <Flow f={f} type="check" start={401} end={437}/><Flow f={f} type="response" start={440} end={472}/>
    <Flow f={f} type="download" start={475} end={538}/>
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
