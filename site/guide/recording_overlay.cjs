// Recording-only compositor overlay; never loaded by the public console.
function overlayScript() {
  let position, mark = 0;
  function mount() {
    if (!document.documentElement) return;
    if (!document.getElementById("guide-pointer")) {
      const cursor = document.createElementNS("http://www.w3.org/2000/svg", "svg"); cursor.id = "guide-pointer";
      cursor.setAttribute("viewBox", "0 0 24 32"); cursor.setAttribute("width", "30"); cursor.setAttribute("height", "40"); cursor.setAttribute("aria-hidden", "true");
      Object.assign(cursor.style, { position: "fixed", left: "0", top: "0", zIndex: "2147483647", pointerEvents: "none", display: "none", filter: "drop-shadow(0 1px 2px rgba(0,0,0,.7))" });
      const arrow = document.createElementNS(cursor.namespaceURI, "path");
      arrow.setAttribute("d", "M2 2v24l6-6 5 10 5-2-5-10 9-1Z"); arrow.setAttribute("fill", "white"); arrow.setAttribute("stroke", "#0b1628"); arrow.setAttribute("stroke-width", "1.8"); arrow.setAttribute("stroke-linejoin", "round");
      cursor.appendChild(arrow); document.documentElement.appendChild(cursor);
    }
  }
  function move(x, y) {
    position = { x, y }; mount(); const cursor = document.getElementById("guide-pointer"); if (!cursor) return;
    cursor.style.left = `${x - 2.5}px`; cursor.style.top = `${y - 2.5}px`; cursor.style.display = "block";
  }
  // Instruction timing is recorded by record_guide.cjs; visible subtitles are native WebVTT.
  window.__guideCaption = () => {};
  window.__guideMark = (x, y) => {
    mount(); const ring = document.createElement("div"); ring.id = `guide-click-${++mark}`;
    Object.assign(ring.style, { position: "fixed", left: `${x - 18}px`, top: `${y - 18}px`, width: "36px", height: "36px", boxSizing: "border-box", borderRadius: "50%", border: "3px solid #f59e0b", background: "rgba(245,158,11,.25)", zIndex: "2147483646", pointerEvents: "none" });
    document.documentElement.appendChild(ring); setTimeout(() => ring.remove(), 700);
  };
  addEventListener("pointermove", event => move(event.clientX, event.clientY), true);
  addEventListener("pointerdown", event => { move(event.clientX, event.clientY); window.__guideMark(event.clientX, event.clientY); }, true);
  addEventListener("DOMContentLoaded", () => { mount(); if (position) move(position.x, position.y); }, { once: true });
  mount();
}
module.exports = { overlayScript };
