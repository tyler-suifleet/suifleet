/* Read-only DOM/history navigation and same-origin media; no wallet, storage or analytics. */
(() => {
  "use strict";
  const root = document.querySelector("[data-guide-player]");
  if (!root) return;
  const stages = [...root.querySelectorAll(".guide-stage")];
  const records = stages.map(stage => [...stage.querySelectorAll("[data-guide-instruction]")].map(item => ({ label: item.dataset.label, text: item.textContent.trim() })));
  const select = root.querySelector("[data-guide-select]");
  const back = root.querySelector("[data-guide-back]");
  const next = root.querySelector("[data-guide-next]");
  const status = root.querySelector("[data-guide-status]");
  const progress = root.querySelector("[data-guide-progress]");
  const complete = root.querySelector("[data-guide-complete]");
  let stageIndex = 0, instruction = 0, part = 0, chunks = [], finished = false;
  const clamp = (value, maximum) => Math.max(0, Math.min(Number.isInteger(value) ? value : 0, maximum));
  function readLocation() {
    const [id, item, page] = location.hash.slice(1).split("/");
    finished = id === "complete";
    stageIndex = Math.max(0, stages.findIndex(stage => stage.id === id));
    instruction = clamp(Number(item), records[stageIndex].length - 1);
    part = clamp(Number(page), 1000);
  }
  // Divide long instructions by the available space, never by a fixed character count.
  // Every word remains accessible with Next, including in short landscape windows.
  function fitText(copy, paragraph, text) {
    const words = text.split(/\s+/), pages = [];
    let offset = 0;
    while (offset < words.length) {
      let low = 1, high = words.length - offset, best = 1;
      while (low <= high) {
        const middle = Math.floor((low + high) / 2);
        paragraph.textContent = words.slice(offset, offset + middle).join(" ");
        if (copy.scrollHeight <= copy.clientHeight && copy.scrollWidth <= copy.clientWidth) { best = middle; low = middle + 1; }
        else high = middle - 1;
      }
      pages.push(words.slice(offset, offset + best).join(" ")); offset += best;
    }
    return pages;
  }
  function render(updateLocation = false) {
    const active = document.activeElement;
    const wasComplete = !complete.hidden;
    stages.forEach((stage, index) => {
      stage.hidden = finished || index !== stageIndex;
      if (stage.hidden) stage.querySelectorAll("video").forEach(video => video.pause());
      else stage.querySelectorAll("video").forEach(video => {
        if (!video.dataset.guideLoaded) { video.dataset.guideLoaded = "true"; video.preload = "metadata"; video.load(); }
      });
    });
    complete.hidden = !finished;
    select.value = finished ? "complete" : stages[stageIndex].id;
    if (finished) {
      status.textContent = "Walkthrough complete"; progress.value = stages.length; next.textContent = "Start again ↻";
    } else {
      const stage = stages[stageIndex], record = records[stageIndex][instruction];
      const copy = stage.querySelector(".guide-copy"), paragraph = copy.querySelector("p");
      stage.querySelector("[data-guide-label]").textContent = record.label;
      chunks = fitText(copy, paragraph, record.text);
      part = part < 0 ? chunks.length - 1 : clamp(part, chunks.length - 1);
      paragraph.textContent = chunks[part];
      stage.dataset.instruction = instruction; stage.dataset.part = part;
      stage.querySelector("[data-guide-part]").textContent = `Instruction ${instruction + 1} of ${records[stageIndex].length}${chunks.length > 1 ? ` · Part ${part + 1} of ${chunks.length}` : ""}`;
      status.textContent = `Stage ${stageIndex + 1} of ${stages.length} · ${stage.dataset.title}`;
      stage.querySelector("[data-guide-prev-part]").disabled = instruction === 0 && part === 0;
      stage.querySelector("[data-guide-forward]").disabled = instruction === records[stageIndex].length - 1 && part === chunks.length - 1;
      progress.value = stageIndex + 1;
      next.textContent = stageIndex === stages.length - 1 ? "Finish guide ✓" : "Next stage →";
    }
    back.disabled = !finished && stageIndex === 0;
    // Disabled or newly hidden controls cannot retain keyboard focus.
    if (finished && !wasComplete) complete.focus();
    else if (active && root.contains(active) && (active.disabled || active.closest("[hidden]"))) {
      (finished ? complete : stages[stageIndex].querySelector("h2")).focus();
    }
    if (updateLocation) history.pushState(null, "", finished ? "#complete" : `#${stages[stageIndex].id}/${instruction}/${part}`);
  }
  back.addEventListener("click", () => {
    if (finished) { finished = false; stageIndex = stages.length - 1; }
    else stageIndex = Math.max(0, stageIndex - 1);
    instruction = 0; part = 0; render(true);
  });
  next.addEventListener("click", () => {
    if (finished) { finished = false; stageIndex = 0; }
    else if (stageIndex < stages.length - 1) stageIndex++;
    else finished = true;
    instruction = 0; part = 0; render(true);
  });
  stages.forEach(stage => {
    stage.querySelector("[data-guide-forward]").addEventListener("click", () => {
      if (part < chunks.length - 1) part++;
      else if (instruction < records[stageIndex].length - 1) { instruction++; part = 0; }
      render(true);
    });
    stage.querySelector("[data-guide-prev-part]").addEventListener("click", () => {
      if (part > 0) part--;
      else if (instruction > 0) { instruction--; part = -1; }
      render(true);
    });
  });
  select.addEventListener("change", () => {
    finished = select.value === "complete";
    stageIndex = Math.max(0, stages.findIndex(stage => stage.id === select.value)); instruction = 0; part = 0; render(true);
  });
  addEventListener("popstate", () => { readLocation(); render(); });
  addEventListener("hashchange", () => { readLocation(); render(); });
  let resize;
  addEventListener("resize", () => { clearTimeout(resize); resize = setTimeout(() => { part = 0; render(); }, 80); });
  root.querySelectorAll("video").forEach(video => video.addEventListener("error", () => {
    video.hidden = true; video.closest("figure").querySelector("[data-video-error]").hidden = false;
  }));
  root.querySelectorAll(".guide-instructions").forEach(list => { list.hidden = true; });
  document.body.classList.add("guide-ready");
  select.disabled = false; next.disabled = false; readLocation(); render();
  document.fonts.ready.then(() => render());
})();
