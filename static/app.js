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
    if (payload.code === "session_expired") {
      document.getElementById("session-login").href = "/login/?next=" + encodeURIComponent(location.pathname + location.search);
      document.getElementById("session-notice").hidden = false;
      const error = new Error(payload.detail); error.sessionExpired = true; throw error;
    }
    if (!response.ok) throw new Error(payload.detail || "Не удалось выполнить запрос. Обнови страницу и попробуй ещё раз.");
    if (!type.includes("application/json")) throw new Error("Неожиданный ответ. Обнови страницу и проверь, что ты вошёл в кабинет.");
    return payload;
  } catch (error) {
    if (error.name === "AbortError") throw new Error("Запрос занял слишком много времени. Попробуй ещё раз.");
    throw error;
  } finally { clearTimeout(timer); }
}
document.querySelectorAll(".demo-fill").forEach(button => button.addEventListener("click", () => {
  document.getElementById("id_username").value = button.dataset.login;
  document.getElementById("id_password").value = button.dataset.password;
  document.querySelector('#login-form [name="demo_entry"]').value = "1";
  document.getElementById("id_username").form.requestSubmit();
}));
const passwordToggle = document.getElementById("password-toggle");
passwordToggle?.addEventListener("click", () => {
  const input = document.getElementById("id_password");
  const visible = input.type === "password";
  input.type = visible ? "text" : "password";
  passwordToggle.textContent = visible ? "Скрыть" : "Показать";
  passwordToggle.setAttribute("aria-label", visible ? "Скрыть пароль" : "Показать пароль");
  passwordToggle.setAttribute("aria-pressed", String(visible));
});
document.getElementById("login-form")?.addEventListener("submit", event => {
  event.target.setAttribute("aria-busy", "true");
  document.querySelectorAll('#login-form [type="submit"], .demo-fill').forEach(button => button.disabled = true);
});
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
  refresh.disabled = true; status.textContent = "Учитываем навыки, цель и историю. Подбор может занять до 10 секунд…"; uncovered.hidden = true;
  box.setAttribute("aria-busy", "true");
  try {
    const data = await postJSON(recommendationSection.dataset.recommendationsUrl);
    status.innerHTML = `<span class="mode">${escapeHTML(data.mode_label)}</span>${data.steps.length ? `<span>${escapeHTML(data.notice)}</span>` : ""}<span>${(data.elapsed_ms / 1000).toFixed(2)} с${data.cached ? " · сохранённый подбор" : ""}</span>`;
    box.innerHTML = data.steps.length ? data.steps.map((step, index) => `
      <article class="recommendation-card">
        <div class="card-top"><span class="step-icon">${["↗","◇","✧"][index]}</span><span class="step-number">ШАГ 0${index+1}</span></div>
        <h3>${escapeHTML(step.title)}</h3>
        <div class="card-meta"><span>${escapeHTML(step.format)}</span><span>·</span><span>${escapeHTML(step.duration_hours)} ч</span></div>
        <p class="priority">${escapeHTML(step.priority)}</p>
        <div class="step-benefits"><span>Ожидаемый прирост после подтверждения</span>${step.benefits.map(b => `<p><strong>${escapeHTML(b.name)}</strong><b>${escapeHTML(b.before)} → ${escapeHTML(b.after)}</b></p>`).join("")}</div>
        <p class="history-evidence">${escapeHTML(step.explanation.find(f => f.category === "history")?.text)}</p>
        <details class="step-explanation"><summary>Почему этот шаг подходит</summary><ul class="evidence">${step.explanation.map(f => `<li>${escapeHTML(f.text)}</li>`).join("")}</ul></details>
        <p class="card-session">Ближайшая возможность: ${escapeHTML(step.next_session)}</p>
        <div class="card-actions"><a class="button secondary" href="/people/${encodeURIComponent(recommendationSection.dataset.employeeId)}/events/${encodeURIComponent(step.event_id)}/">Подробнее</a>
        ${recommendationSection.dataset.canManage === "true" ? `<button class="button primary" data-plan-url="/api/people/${encodeURIComponent(recommendationSection.dataset.employeeId)}/plan/${encodeURIComponent(step.event_id)}/add/" data-message="Шаг добавлен в личный план.">В мой план +</button>` : ""}</div>
      </article>`).join("") : renderEmptyRecommendations(data);
    if (data.uncovered.length) {
      uncovered.innerHTML = `<details><summary>Какие разрывы пока не закрыты новыми рекомендациями · ${data.uncovered.length}</summary><p>${escapeHTML(data.uncovered.join(", "))}.</p><p>Часть шагов может быть уже в плане. Остальные потребности можно обсудить с координатором развития.</p></details>`;
      uncovered.hidden = false;
    }
  } catch (error) {
    status.textContent = error.message;
    box.innerHTML = error.sessionExpired ? '<div class="empty-state">Войди в кабинет по ссылке в уведомлении, чтобы продолжить подбор.</div>' : '<div class="empty-state">Подбор временно недоступен. Нажми «Обновить подбор», чтобы повторить.</div>';
  } finally { refresh.disabled = false; box.setAttribute("aria-busy", "false"); }
}
function renderEmptyRecommendations(data) {
  const titles = {no_goal:"Выбери, куда хочешь развиваться", goal_met:"Требования цели по навыкам достигнуты", plan_active:"Продолжи свой план", catalog_gap:"Нужен следующий шаг вне каталога"};
  const root = "/people/" + encodeURIComponent(recommendationSection.dataset.employeeId);
  let links = "";
  if (data.empty_reason === "no_goal" && recommendationSection.dataset.canManage === "true") links = '<a class="button secondary" href="#career-goal" data-open-goal>Выбрать цель ↗</a>';
  if (data.empty_reason === "plan_active") links = `<a class="button primary" href="${root}/plan/">Открыть мой план ↗</a>`;
  if (data.empty_reason === "catalog_gap") links = `<a class="button secondary" href="${root}/requests/">${recommendationSection.dataset.canManage === "true" ? "Запросить практику" : "Посмотреть запросы сотрудника"} ↗</a>`;
  return `<div class="empty-state"><strong>${escapeHTML(titles[data.empty_reason] || "Новых рекомендаций пока нет")}</strong><p>${escapeHTML(data.notice)}</p>${links}</div>`;
}
document.addEventListener("click", event => {
  if (event.target.closest("[data-open-goal]")) document.getElementById("career-goal").open = true;
});
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
    await postJSON(button.dataset.planUrl);
    const message = button.dataset.message;
    sessionStorage.setItem("careerquest-message", message || "План обновлён.");
    if (button.dataset.redirect) location.assign(button.dataset.redirect);
    else location.reload();
  } catch (error) { toast(error.message); button.disabled = false; button.textContent = previousText; }
});
const savedMessage = sessionStorage.getItem("careerquest-message");
if (savedMessage) { sessionStorage.removeItem("careerquest-message"); toast(savedMessage); }
