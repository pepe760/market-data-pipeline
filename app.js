"use strict";
const $ = id => document.getElementById(id);
const REPO = "pepe760/market-data-pipeline";
const TITLES = {overview:"收集總覽", inventory:"資料庫存", trends:"走勢比較", reports:"回測報告"};
const WORKFLOWS = new Map([
  ["daily.yml","美股每日價格及事件"], ["options.yml","美股期權日快照"],
  ["intraday15m.yml","核心標的 15 分鐘"], ["broad_stocks.yml","美股廣覆蓋 15 分鐘"],
  ["broad_crypto_hourly.yml","加密貨幣每小時"], ["broad_crypto_options.yml","加密貨幣期權"],
  ["xsp15m.yml","XSP 期權 15 分鐘"]
]);
const STATUS = {
  success:["成功","good"], failure:["失敗","bad"], timed_out:["超時","bad"], cancelled:["取消","warn"],
  skipped:["略過","warn"], in_progress:["執行中",""], queued:["排隊中",""], requested:["已要求",""],
  COMPLETE:["完整","good"], PARTIAL:["有缺口","warn"], DATA_PARTIAL:["有缺口","warn"],
  NO_VERIFIED_15M_SCAN:["待接駁報價","warn"], CAPTURED_DELAYED:["延遲快照","good"],
  NO_LOCAL_BATCHES:["未有本機批次","warn"], LOCAL_DATA_MISSING:["未有本機資料","warn"],
  COMPLETE_WITH_VENDOR_DEFECTS:["有來源缺口","warn"], NO_QUOTES:["冇報價","warn"]
};
const integer = new Intl.NumberFormat("en-HK");
const hkt = new Intl.DateTimeFormat("en-GB",{dateStyle:"medium",timeStyle:"short",timeZone:"Asia/Hong_Kong"});
const fmt = value => Number.isFinite(value) ? integer.format(value) : "—";
const date = value => {
  if (!value) return "—";
  if (/^\d{4}-\d{2}(-\d{2})?$/.test(value)) return value;
  const stamp = new Date(value);
  return Number.isNaN(+stamp) ? String(value) : hkt.format(stamp);
};
const pct = value => `${value > 0 ? "+" : ""}${value.toFixed(2)}%`;
const state = {snapshot:null, filter:"all", sort:0, pins:new Set(), page:1, actionRequest:0, importRequest:0};
function el(tag,text,classes) {
  const node = document.createElement(tag);
  if (text !== undefined && text !== null) node.textContent = text;
  if (classes) node.className = classes;
  return node;
}
function badge(value) {
  const item = STATUS[value] || (/^LOCAL_.+_PRESENT$/.test(value || "") ? ["本機有存檔","good"] : [value || "待核對","warn"]);
  return el("span",item[0],`tag ${item[1]}`);
}
function link(text,href) {
  return Object.assign(el("a",text),{href,target:"_blank",rel:"noopener noreferrer"});
}
function appendRow(target,...cells) {
  const row = el("tr");
  for (const cell of cells) {
    const td = el("td");
    if (cell instanceof Node) td.append(cell);
    else td.textContent = cell ?? "—";
    row.append(td);
  }
  target.append(row);
}
function stat(title,value,note) {
  const block = el("div",null,"stat");
  block.append(el("p",title,"label"),el("strong",value),el("p",note));
  return block;
}
function notice(message) {
  $("notice").textContent = message;
  $("notice").hidden = !message;
}
function validSnapshot(doc) {
  const object = x => !!x && typeof x === "object" && !Array.isArray(x);
  if (!object(doc) || doc.schema_version !== 1 || doc.kind !== "market-data-lab-private-snapshot"
      || typeof doc.generated_at !== "string" || !object(doc.inventory) || !object(doc.reports)) return false;
  const lanes = doc.inventory.sources, reports = doc.reports.items;
  return Array.isArray(lanes) && lanes.length <= 100 && lanes.every(s => object(s) && typeof s.id === "string"
        && typeof s.label === "string" && Number.isSafeInteger(s.batch_count) && s.batch_count >= 0)
    && Array.isArray(reports) && reports.length <= 10000 && reports.every(r => object(r)
        && typeof r.title === "string" && typeof r.drive_url === "string")
    && Array.isArray(doc.reports.groups) && doc.reports.groups.every(g => object(g)
        && typeof g.title === "string" && ["string","number"].includes(typeof g.id))
    && Array.isArray(doc.trends) && doc.trends.length <= 100 && doc.trends.every(t => object(t)
        && typeof t.symbol === "string" && typeof t.lane === "string" && Array.isArray(t.points)
        && t.points.length <= 1000 && t.points.every(p => object(p) && typeof p.time === "string"
            && Number.isFinite(p.close) && p.close > 0));
}
async function importSnapshot(file) {
  if (!file) return;
  const id = ++state.importRequest;
  try {
    if (file.size > 5_000_000) throw new Error("檔案大過 5 MB");
    const doc = JSON.parse(await file.text());
    if (!validSnapshot(doc)) throw new Error("檔案格式同研究台快照唔吻合");
    if (id !== state.importRequest) return;
    state.snapshot = doc;
    state.page = 1;
    state.pins.clear();
    $("snapshot-badge").className = "tag good";
    $("snapshot-badge").textContent = "私人快照已載入";
    $("clear-snapshot").hidden = false;
    notice(`已載入快照 · ${date(doc.generated_at)} 香港時間`);
    prepareOptions();
    render();
  } catch (error) {
    if (id === state.importRequest) notice(`讀唔到呢份快照：${error instanceof SyntaxError ? "JSON 格式有誤" : error.message}`);
  }
}
function clearSnapshot() {
  state.importRequest++;
  state.snapshot = null;
  state.page = 1;
  state.pins.clear();
  $("snapshot").value = "";
  $("snapshot-badge").className = "tag warn";
  $("snapshot-badge").textContent = "未載入私人快照";
  $("clear-snapshot").hidden = true;
  notice("");
  prepareOptions();
  render();
}
function renderOverview() {
  const data = state.snapshot;
  const lanes = data?.inventory.sources || [];
  const count = lanes.reduce((total,s)=>total + s.batch_count,0);
  const archives = data?.reports.stats?.verified_versions;
  $("overview-stats").replaceChildren(stat("INVENTORY",data?fmt(lanes.length):"—","資料系列"),
    stat("COLLECTION",data?fmt(count):"—","已登記批次"),stat("RESEARCH",fmt(archives),"已驗證報告版本"));
  const xsp = lanes.find(s=>s.id==="xsp15m");
  $("focus-note").textContent = xsp ? `${badge(xsp.quality).textContent} · ${fmt(xsp.metrics?.distinct_target_slots_with_quotes || 0)} 個有報價時段`
    : "美股正常交易時段，追蹤當日及下一交易日到期鏈。載入私人快照睇收集覆蓋。";
}
function renderInventory() {
  const rows = $("inventory-rows");
  rows.replaceChildren();
  const query = $("filter").value.trim().toLocaleLowerCase();
  const items = (state.snapshot?.inventory.sources || []).filter(s => {
    if (state.filter === "pinned" ? !state.pins.has(s.id) : state.filter !== "all" && s.group !== state.filter) return false;
    return [s.id,s.label,s.source,s.cadence,...(Array.isArray(s.examples)?s.examples:[])].some(x=>String(x||"").toLocaleLowerCase().includes(query));
  });
  if (state.sort) items.sort((a,b)=>state.sort*(b.batch_count-a.batch_count));
  for (const item of items) {
    const label = el("div");
    const title = el("strong");
    const starred = state.pins.has(item.id);
    const pin = el("button",starred?"★":"☆","star");
    pin.setAttribute("aria-pressed",starred);
    pin.setAttribute("aria-label",`標星 ${item.label}`);
    pin.addEventListener("click",()=>{starred?state.pins.delete(item.id):state.pins.add(item.id);renderInventory();});
    title.append(pin,document.createTextNode(item.label));
    label.append(title,el("small",[item.source,item.drive_path].filter(Boolean).join(" · ")),el("small",item.coverage_note));
    const coverage = item.coverage_start ? `${date(item.coverage_start)} → ${date(item.coverage_end)}` : "—";
    appendRow(rows,label,item.cadence,coverage,item.group==="historical"?"—":fmt(item.batch_count),badge(item.quality));
  }
  $("inventory-clock").textContent = state.snapshot ? `快照 ${date(state.snapshot.generated_at)}` : "";
  $("inventory-state").textContent = state.snapshot ? `${fmt(items.length)} 個系列。覆蓋顯示已存觀測嘅最早同最遲時間，個別時段可以有缺口。`
    : "從私人 Drive 下載 JSON，再按右上角「載入私人快照」。";
}
function prepareOptions() {
  const trends = state.snapshot?.trends || [];
  const order = [...trends.keys()].sort((a,b)=>(trends[b].symbol==="SPY"&&trends[b].lane==="daily")-(trends[a].symbol==="SPY"&&trends[a].lane==="daily"));
  $("trend-main").replaceChildren(...order.map(i=>new Option(`${trends[i].symbol} · ${trends[i].cadence || trends[i].lane}`,i)));
  prepareComparison();
  $("report-group").replaceChildren(new Option("全部",""),...(state.snapshot?.reports.groups || []).map(g=>new Option(g.title,g.id)));
}
function prepareComparison() {
  const trends = state.snapshot?.trends || [];
  const main = trends[$("trend-main").value];
  $("trend-compare").replaceChildren(new Option("單一系列",""),...trends.flatMap((t,i) =>
    t !== main && t.lane === main?.lane ? [new Option(t.symbol,i)] : []));
}
function svgEl(tag,attrs = {},text) {
  const result = document.createElementNS("http://www.w3.org/2000/svg",tag);
  for (const [key,value] of Object.entries(attrs)) result.setAttribute(key,value);
  if (text !== undefined) result.textContent = text;
  return result;
}
function renderTrends() {
  $("trend-chart").replaceChildren();
  $("trend-summary").replaceChildren();
  $("trend-cursor").textContent = "";
  const trends = state.snapshot?.trends || [];
  const primary = trends[$("trend-main").value];
  const other = $("trend-compare").value === "" ? null : trends[$("trend-compare").value];
  if (!primary) {
    $("trend-state").textContent = "載入快照先可以睇已存價格。";
    return;
  }
  const otherPoints = other ? new Map(other.points.map(p=>[p.time,p])) : null;
  const points = primary.points.filter(p=>!otherPoints || otherPoints.has(p.time)).slice(-Number($("range").value));
  if (points.length < 2) {
    $("trend-state").textContent = "兩個系列嘅共同觀測點不足，請試另一個比較。";
    return;
  }
  const series = [[primary,points,"#84f3c9"]];
  if (other) series.push([other,points.map(p=>otherPoints.get(p.time)),"#86b7ff"]);
  const lines = series.map(([meta,rows,color])=>({symbol:meta.symbol,color,points:rows.map(row=>({
    time:row.time,index:row.close/rows[0].close*100,gap:!!row.gap_before
  }))}));
  const returns = lines.map(item=>item.points.at(-1).index - 100);
  $("trend-summary").replaceChildren(...lines.map((item,i)=>stat(item.symbol,pct(returns[i]),"區間價格升跌")),
    stat("OBSERVATIONS",fmt(points.length),other?"共同觀測點":"觀測點"));
  const all = lines.flatMap(line=>line.points.map(p=>p.index));
  let low = Math.min(...all), high = Math.max(...all);
  const margin = (high - low || 2) * .1;
  low -= margin; high += margin;
  const W=880,H=310,L=15,R=812,T=12,B=270;
  const X = i => L + i/(points.length-1)*(R-L);
  const Y = value => B - (value-low)/(high-low)*(B-T);
  const svg = svgEl("svg",{viewBox:`0 0 ${W} ${H}`,role:"img",tabindex:"0",
    "aria-label":`${lines.map(l=>l.symbol).join(" 及 ")} 基準 100 走勢圖`});
  for (let i=0;i<5;i++) {
    const value=low+(high-low)*i/4;
    svg.append(svgEl("line",{x1:L,x2:R,y1:Y(value),y2:Y(value)}),
      svgEl("text",{x:R+8,y:Y(value)+4},value.toFixed(2)));
  }
  for (const line of lines) {
    const data = line.points.map((p,i)=>`${!i || p.gap?"M":"L"}${X(i).toFixed(2)},${Y(p.index).toFixed(2)}`).join(" ");
    svg.append(svgEl("path",{d:data,stroke:line.color}));
  }
  svg.append(svgEl("text",{x:L,y:B+23},points[0].time.slice(0,10)),
    svgEl("text",{x:R,y:B+23,"text-anchor":"end"},points.at(-1).time.slice(0,10)));
  const cursor = svgEl("line",{y1:T,y2:B,"stroke-dasharray":"4 4"});
  const dots = lines.map(l=>svgEl("circle",{r:4,fill:l.color}));
  svg.append(cursor,...dots);
  let selected = points.length-1;
  function focus(i) {
    cursor.setAttribute("x1",X(i)); cursor.setAttribute("x2",X(i));
    for (const [j,dot] of dots.entries()) {dot.setAttribute("cx",X(i));dot.setAttribute("cy",Y(lines[j].points[i].index));}
    $("trend-cursor").textContent = `${date(points[i].time)} · ${lines.map(l=>l.symbol+" "+l.points[i].index.toFixed(2)).join(" · ")}`;
  }
  svg.addEventListener("pointermove",event=>{
    const rect=svg.getBoundingClientRect(), relative=(event.clientX-rect.left)*W/rect.width;
    selected=Math.max(0,Math.min(points.length-1,Math.round((relative-L)/(R-L)*(points.length-1))));
    focus(selected);
  });
  svg.addEventListener("keydown",event=>{
    if(!["ArrowLeft","ArrowRight"].includes(event.key))return;
    event.preventDefault();
    selected=Math.max(0,Math.min(points.length-1,selected+(event.key==="ArrowRight"?1:-1)));
    focus(selected);
  });
  $("trend-chart").append(svg);
  focus(selected);
  $("trend-state").textContent = "圖表由所選區間第一個價格點設為 100，比較只用相同頻率嘅共同時點。";
}
function renderReports() {
  const body=$("reports-rows");
  body.replaceChildren();
  const q=$("report-search").value.toLocaleLowerCase().trim().split(/\s+/).filter(Boolean);
  const group=$("report-group").value;
  const source=state.snapshot?.reports.items || [];
  const matches=source.filter(r=>r.kind==="reports" && (!group || group===String(r.group))
    && q.every(term=>[r.title,r.project,r.group_title].some(value=>String(value||"").toLocaleLowerCase().includes(term))));
  const limit=16, pages=Math.max(1,Math.ceil(matches.length/limit));
  state.page=Math.min(state.page,pages);
  for (const r of matches.slice((state.page-1)*limit,state.page*limit)) {
    const safe=/^https:\/\/drive\.google\.com\/file\/d\/[A-Za-z0-9_-]+\/view$/.test(r.drive_url);
    appendRow(body,r.title,r.project || r.group_title,safe?link("私人 Drive ↗",r.drive_url):"—");
  }
  $("reports-state").textContent=state.snapshot?`${fmt(matches.length)} 個結果 · ${state.page} / ${pages}`:"載入私人快照後可搜尋。";
  $("prev").disabled=state.page<=1;
  $("next").disabled=state.page>=pages;
}
async function loadActions() {
  const ticket=++state.actionRequest;
  $("refresh-actions").disabled=true;
  $("actions-state").textContent="讀取公開 Actions…";
  try {
    const response=await fetch(`https://api.github.com/repos/${REPO}/actions/runs?branch=main&per_page=100`,
      {credentials:"omit",cache:"no-cache",referrerPolicy:"no-referrer"});
    if(!response.ok)throw new Error("GitHub 狀態暫時未能讀取");
    const data=await response.json(), recent=new Map();
    for(const run of data.workflow_runs || []) {
      const name=String(run.path||"").split("/").at(-1).split("@")[0];
      if(WORKFLOWS.has(name)&&!recent.has(name)&&["schedule","workflow_dispatch"].includes(run.event)&&Number.isSafeInteger(run.id))recent.set(name,run);
    }
    if(ticket!==state.actionRequest)return;
    $("actions").replaceChildren();
    for(const [file,title] of WORKFLOWS) {
      const run=recent.get(file);
      appendRow($("actions"),title,run?badge(run.conclusion||run.status):badge(file==="xsp15m.yml"?"NO_VERIFIED_15M_SCAN":null),
        run?date(run.created_at):"—",run?link("Actions ↗",`https://github.com/${REPO}/actions/runs/${run.id}`):"—");
    }
    $("actions-state").textContent=`公開狀態查詢時間 ${date(new Date())} 香港`;
  } catch {
    if(ticket===state.actionRequest) {
      $("actions-state").replaceChildren(document.createTextNode("GitHub 狀態暫時未能讀取。"),link("開 Actions ↗",`https://github.com/${REPO}/actions`));
    }
  } finally { if(ticket===state.actionRequest)$("refresh-actions").disabled=false; }
}
function render() {
  renderOverview();renderInventory();renderTrends();renderReports();
}
function route() {
  const name=location.hash.slice(1);
  const active=Object.hasOwn(TITLES,name)?name:"overview";
  $("view-title").textContent=TITLES[active];
  document.querySelectorAll(".view").forEach(view=>view.hidden=view.id!==active);
  document.querySelectorAll("[data-view]").forEach(nav=>{
    if(nav.dataset.view===active)nav.setAttribute("aria-current","page");else nav.removeAttribute("aria-current");
  });
  window.scrollTo(0,0);
}
$("snapshot").addEventListener("change",event=>importSnapshot(event.target.files[0]));
$("clear-snapshot").addEventListener("click",clearSnapshot);
$("refresh-actions").addEventListener("click",loadActions);
$("filter").addEventListener("input",renderInventory);
$("inventory-filters").addEventListener("click",event=>{
  const button=event.target.closest("button[data-kind]");
  if(!button)return;
  state.filter=button.dataset.kind;
  document.querySelectorAll("button[data-kind]").forEach(control=>control.classList.toggle("selected",control===button));
  renderInventory();
});
$("sort-batches").addEventListener("click",()=>{state.sort=state.sort===1?-1:1;renderInventory();});
$("trend-main").addEventListener("change",()=>{prepareComparison();renderTrends();});
$("trend-compare").addEventListener("change",renderTrends);
$("range").addEventListener("change",renderTrends);
for(const key of ["report-search","report-group"])$(key).addEventListener("input",()=>{state.page=1;renderReports();});
$("prev").addEventListener("click",()=>{state.page--;renderReports();});
$("next").addEventListener("click",()=>{state.page++;renderReports();});
window.addEventListener("hashchange",route);
render();route();loadActions();
