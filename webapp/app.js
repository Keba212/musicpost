const telegram = window.Telegram?.WebApp;
const queueElement = document.querySelector("#queue");
const countElement = document.querySelector("#count");
const statusElement = document.querySelector("#status");
const apiHeaders = {
  "Content-Type": "application/json",
  "X-Telegram-Init-Data": telegram?.initData || "",
};

function showStatus(message, kind = "info") {
  statusElement.textContent = message;
  statusElement.className = `status ${kind}`;
  statusElement.hidden = false;
  window.setTimeout(() => { statusElement.hidden = true; }, 2600);
}

function renderQueue(items) {
  countElement.textContent = items.length;
  if (!items.length) {
    queueElement.innerHTML = '<div class="empty"><div class="empty-mark">✦</div><h2>Черга порожня</h2><p>Надішли аудіо боту, і воно з’явиться тут.</p></div>';
    return;
  }
  queueElement.innerHTML = items.map((item, index) => `
    <article class="track">
      <div class="track-index">${String(index + 1).padStart(2, "0")}</div>
      <div class="track-info">
        <strong>${escapeHtml(item.name)}</strong>
        <span>${escapeHtml(item.artist)}</span>
      </div>
      <button class="track-publish" data-id="${item.id}" type="button" aria-label="Опублікувати трек">▶</button>
    </article>
  `).join("");
  document.querySelectorAll(".track-publish").forEach((button) => {
    button.addEventListener("click", () => publish(button.dataset.id));
  });
}

function escapeHtml(value) {
  return String(value).replace(/[&<>'"]/g, (character) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#39;", '"': "&quot;",
  }[character]));
}

async function loadQueue() {
  try {
    const response = await fetch("/api/queue", { headers: apiHeaders });
    if (!response.ok) throw new Error("Не вдалося завантажити чергу");
    const data = await response.json();
    renderQueue(data.items);
  } catch (error) {
    showStatus(error.message, "error");
  }
}

async function publish(id = null) {
  try {
    const response = await fetch("/api/publish", {
      method: "POST",
      headers: apiHeaders,
      body: JSON.stringify(id ? { id: Number(id) } : {}),
    });
    if (!response.ok) throw new Error("Публікація не вдалася");
    const data = await response.json();
    showStatus(data.published ? "Трек опубліковано" : "Черга порожня", data.published ? "success" : "info");
    await loadQueue();
  } catch (error) {
    showStatus(error.message, "error");
  }
}

async function clearQueue() {
  if (!window.confirm("Очистити всі треки з черги?")) return;
  try {
    const response = await fetch("/api/clear", { method: "POST", headers: apiHeaders });
    if (!response.ok) throw new Error("Не вдалося очистити чергу");
    showStatus("Чергу очищено", "success");
    await loadQueue();
  } catch (error) {
    showStatus(error.message, "error");
  }
}

document.querySelector("#refresh").addEventListener("click", loadQueue);
document.querySelector("#publish").addEventListener("click", () => publish());
document.querySelector("#clear").addEventListener("click", clearQueue);
telegram?.ready();
telegram?.expand();
loadQueue();
