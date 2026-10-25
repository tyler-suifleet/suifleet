import type { DataRow } from './data-browser';
type Discovery={devices:string[];cursor?:string|null;hasNextPage:boolean};
type Items={rows:DataRow[];cursor?:string|null;hasNextPage:boolean};
/** One bounded read per click. No automatic fleet crawl or query-cache retention. */
export function dataPages(discover:(cursor?:string|null)=>Promise<Discovery>,scan:(device:string,cursor?:string|null)=>Promise<Items>){
  let fleetCursor:string|null|undefined,discovered=false,fleetMore=true,deviceIndex=0,loaded=0;
  const writers:{device:string;cursor?:string|null;more:boolean}[]=[];
  return async()=>{
    if(loaded>=512)throw Error('Visit limit reached (512 chunks); reload to restart');
    if((!discovered||deviceIndex>=writers.length)&&fleetMore){
      if(writers.length>=256)throw Error('Device visit limit reached (256); remaining history is unavailable in this visit');
      const page=await discover(fleetCursor);
      if(page.devices.length>100||(page.hasNextPage&&(!page.cursor||page.cursor===fleetCursor)))throw Error('Invalid fleet metadata page');
      if(writers.length+page.devices.length>256)throw Error('Device visit limit reached (256)');
      const known=new Set(writers.map(w=>w.device));
      for(const device of page.devices){if(known.has(device))throw Error('Repeated fleet device');known.add(device);}
      discovered=true;fleetCursor=page.cursor;fleetMore=page.hasNextPage;
      writers.push(...page.devices.map(device=>({device,more:true})));
      return {rows:[],more:fleetMore||writers.some(w=>w.more)};
    }
    if(deviceIndex>=writers.length)deviceIndex=0;
    const writer=writers.slice(deviceIndex).find(w=>w.more)||writers.find(w=>w.more);
    if(!writer)return {rows:[],more:false};
    // A failed device remains pending; the next click can browse another device.
    deviceIndex=writers.indexOf(writer)+1;
    const page=await scan(writer.device,writer.cursor);
    if(page.rows.length>32||(page.hasNextPage&&(!page.cursor||page.cursor===writer.cursor)))throw Error('Invalid item metadata page');
    if(loaded+page.rows.length>512)throw Error('Chunk visit limit reached (512); reload to restart');
    writer.cursor=page.cursor;writer.more=page.hasNextPage;loaded+=page.rows.length;
    return {rows:page.rows,more:fleetMore||writers.some(w=>w.more)};
  };
}
