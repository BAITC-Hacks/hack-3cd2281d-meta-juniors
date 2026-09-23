"use strict";
const escapeHTML = value => String(value ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const csrf = () => document.querySelector('[name="csrfmiddlewaretoken"]')?.value || "";
let toastTimer;
function toast(message) {
  const box = document.getElementById("toast");
  box.textContent = message; box.hidden = false;
  clearTimeout(toastTimer); toastTimer = setTimeout(() => box.hidden = true, 7000);
}
async function postJSON(url, data = {}) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), 12000);
  try {
    const response = await fetch(url, {method:"POST", credentials:"same-origin", signal:controller.signal,
      headers:{"Content-Type":"application/json", "X-CSRFToken":csrf()}, body:JSON.stringify(data)});
    const type = response.headers.get("content-type") || "";
    const payload = type.includes("application/json") ? await response.json() : {};
    if (!response.ok) throw new Error(payload.detail || "Не удалось выполнить запрос. Обнови страницу и попробуй ещё раз.");
    return payload;
  } catch (error) {
    if (error.name === "AbortError") throw new Error("Запрос занял слишком много времени. Попробуй ещё раз.");
    throw error;
  } finally { clearTimeout(timer); }
}
document.querySelectorAll(".demo-fill").forEach(button => button.addEventListener("click", () => {
  document.getElementById("id_username").value = button.dataset.login;
  document.getElementById("id_password").value = button.dataset.password;
  document.getElementById("id_username").form.requestSubmit();
}));
const goalForm = document.getElementById("goal-form");
goalForm?.addEventListener("submit", async event => {
  event.preventDefault(); const button = goalForm.querySelector("button"); button.disabled = true;
  try { await postJSON(goalForm.dataset.url, Object.fromEntries(new FormData(goalForm))); location.reload(); }
  catch (error) { toast(error.message); button.disabled = false; }
});
const recommendationSection = document.querySelector("[data-recommendations-url]");
async function loadRecommendations() {
  const box = document.getElementById("recommendations");
  const status = document.getElementById("recommendation-status");
  const refresh = document.getElementById("refresh-recommendations");
  const uncovered = document.getElementById("uncovered");
  refresh.disabled = true; status.textContent = "Подбираем подходящие шаги…"; uncovered.hidden = true;
  try {
    const data = await postJSON(recommendationSection.dataset.recommendationsUrl);
    status.innerHTML = `<span class="mode">${escapeHTML(data.mode_label)}</span><span>${escapeHTML(data.notice)}</span><span>${(data.elapsed_ms / 1000).toFixed(2)} с${data.cached ? " · сохранённый подбор" : ""}</span>`;
    box.innerHTML = data.steps.length ? data.steps.map((step, index) => `
      <article class="recommendation-card">
        <div class="card-top"><span class="step-icon">${["↗","◇","✧"][index]}</span><span class="step-number">ШАГ 0${index+1}</span></div>
        <h3>${escapeHTML(step.title)}</h3>
        <div class="card-meta"><span>${escapeHTML(step.format)}</span><span>·</span><span>${escapeHTML(step.duration_hours)} ч</span><span>· ${escapeHTML(step.event_id)}</span></div>
        <p class="priority">${escapeHTML(step.priority)}</p>
        <ul class="evidence">${step.explanation.map(f => `<li>${escapeHTML(f.text)}</li>`).join("")}</ul>
        <p class="card-session">Ближайшая возможность: ${escapeHTML(step.next_session)}</p>
        <div class="card-actions"><a class="button secondary" href="/people/${encodeURIComponent(recommendationSection.dataset.employeeId)}/events/${encodeURIComponent(step.event_id)}/">Подробнее</a>
        ${recommendationSection.dataset.canManage === "true" ? `<button class="button primary" data-plan-url="/api/people/${encodeURIComponent(recommendationSection.dataset.employeeId)}/plan/${encodeURIComponent(step.event_id)}/add/" data-message="Шаг добавлен в личный план.">В мой план +</button>` : ""}</div>
      </article>`).join("") : `<div class="empty-state"><strong>${escapeHTML(data.mode_label)}</strong>${escapeHTML(data.notice)}</div>`;
    if (data.uncovered.length) {
      uncovered.textContent = `Для этих разрывов нет новых доступных шагов вне плана: ${data.uncovered.join(", ")}. Проверь личный план или обсуди варианты с HR.`;
      uncovered.hidden = false;
    }
  } catch (error) {
    status.textContent = error.message;
    box.innerHTML = '<div class="empty-state">Подбор временно недоступен. Нажми «Обновить подбор», чтобы повторить.</div>';
  } finally { refresh.disabled = false; }
}
if (recommendationSection) {
  loadRecommendations();
  document.getElementById("refresh-recommendations").addEventListener("click", loadRecommendations);
}
document.addEventListener("click", async event => {
  const button = event.target.closest("[data-plan-url]");
  if (!button || button.disabled) return;
  const previousText = button.textContent;
  button.disabled = true; button.textContent = "Сохраняем…";
  try {
    button.dataset.requestId ||= crypto.randomUUID();
    const result = await postJSON(button.dataset.planUrl, {request_id:button.dataset.requestId});
    const message = button.dataset.completion
      ? result.already_completed ? "Эта операция уже учтена. Повторного начисления нет." : `Шаг завершён. Покрытие навыков: ${result.coverage_before}% → ${result.coverage_after}%.`
      : button.dataset.message;
    sessionStorage.setItem("careerquest-message", message || "План обновлён.");
    if (button.dataset.redirect) location.assign(button.dataset.redirect);
    else location.reload();
  } catch (error) { toast(error.message); button.disabled = false; button.textContent = previousText; }
});
const savedMessage = sessionStorage.getItem("careerquest-message");
if (savedMessage) { sessionStorage.removeItem("careerquest-message"); toast(savedMessage); }

