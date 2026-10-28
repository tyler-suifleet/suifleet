"use strict";
// Search remains on this origin; queries never leave the browser.
const input = document.getElementById("search");
const results = document.getElementById("search-results");
const status = document.getElementById("search-status");
let index;
input.addEventListener("input", async () => {
  const query = input.value.trim().toLowerCase();
  results.replaceChildren();
  status.textContent = "";
  if (!query) return;
  try {
    index ??= fetch("/search.json").then(response => {
      if (!response.ok) throw new Error("Search unavailable");
      return response.json();
    });
    const pages = await index;
    if (query !== input.value.trim().toLowerCase()) return;
    const words = query.split(/\s+/);
    const matches = pages.filter(page => words.every(word => (page.title + " " + page.text).toLowerCase().includes(word))).slice(0, 20);
    status.textContent = matches.length ? matches.length + " matching pages" : "No matching pages. Try fewer words.";
    for (const page of matches) {
      const li = document.createElement("li");
      const a = document.createElement("a");
      a.href = page.path;
      a.textContent = page.title;
      li.append(a);
      results.append(li);
    }
  } catch {
    index = undefined;
    status.textContent = "Search unavailable. Browse the navigation below.";
  }
});
