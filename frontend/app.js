"use strict";
const $ = (id) => document.getElementById(id);
let meetingId = null;
let uploading = false;
let running = false;
let meetingStatus = "created";
let timer = null;
const stages = {transcription: "Распознавание речи", diarization: "Определение говорящих",
  alignment: "Подготовка транскрипта", analysis: "Анализ совещания", validation: "Проверка результата",
  database: "Сохранение результата", export: "Формирование DOCX и PDF", complete: "Готово"};

function node(tag, text, className) {
  const element = document.createElement(tag);
  if (text !== undefined) element.textContent = text;
  if (className) element.className = className;
  return element;
}
function showError(error) { $("error").textContent = error.message; $("error").hidden = false; }
function clearError() { $("error").hidden = true; }
async function api(path, options = {}) {
  let response;
  try { response = await fetch(path, {cache: "no-store", ...options}); }
  catch { throw new Error("Нет связи с сервером. Обновите результат, чтобы проверить состояние обработки."); }
  const body = await response.json().catch(() => ({}));
  if (!response.ok) {
    const detail = body.detail;
    const message = Array.isArray(detail) ? "Проверьте заполненные поля." :
      (typeof detail === "string" ? detail : detail?.message) || `Ошибка сервера (${response.status}).`;
    const error = new Error(message);
    error.status = response.status;
    throw error;
  }
  return body;
}
function controls() {
  $("upload-button").disabled = uploading || running || meetingStatus !== "created";
  $("file").disabled = uploading || running || meetingStatus !== "created";
  $("title").disabled = !!meetingId || uploading;
  $("meeting-date").disabled = !!meetingId || uploading;
  $("analyze").disabled = !meetingId || uploading || running || !["audio_ready", "transcribed"].includes(meetingStatus);
  $("refresh").disabled = !meetingId || uploading;
  $("activity").hidden = !uploading && !running;
}
function selectTab(panel) {
  for (const tab of document.querySelectorAll('[role="tab"]')) {
    const active = tab.dataset.panel === panel;
    tab.setAttribute("aria-selected", String(active));
    tab.tabIndex = active ? 0 : -1;
    $(tab.dataset.panel).hidden = !active;
  }
}
const tabs = [...document.querySelectorAll('[role="tab"]')];
for (const [index, tab] of tabs.entries()) {
  tab.addEventListener("click", () => selectTab(tab.dataset.panel));
  tab.addEventListener("keydown", (event) => {
    let target;
    if (event.key === "ArrowRight") target = tabs[(index + 1) % tabs.length];
    if (event.key === "ArrowLeft") target = tabs[(index + tabs.length - 1) % tabs.length];
    if (event.key === "Home") target = tabs[0];
    if (event.key === "End") target = tabs[tabs.length - 1];
    if (target) { event.preventDefault(); selectTab(target.dataset.panel); target.focus(); }
  });
}
function sources(parent, ids) {
  for (const id of ids) {
    const button = node("button", `Фрагмент ${id}`, "source");
    button.addEventListener("click", () => {
      selectTab("transcript");
      const segment = $(`segment-${id}`);
      if (segment) { segment.focus(); segment.scrollIntoView({block: "center"}); }
    });
    parent.append(button);
  }
}
function renderResult(result) {
  $("result").hidden = false;
  $("result-heading").textContent = result.meeting.title;
  for (const name of ["summary", "actions", "transcript"]) $(name).replaceChildren();
  for (const [key, label] of [["topics", "Темы"], ["key_discussions", "Ключевые обсуждения"],
    ["decisions", "Принятые решения"], ["problems_and_risks", "Проблемы и риски"], ["main_action_items", "Основные поручения"]]) {
    $("summary").append(node("h3", label));
    const points = result.summary[key];
    if (!points.length) $("summary").append(node("p", "Не зафиксированы.", "muted"));
    for (const point of points) {
      const section = node("div");
      section.append(node("p", point.text || point.description));
      if (point.condition) section.append(node("p", `Условие: ${point.condition}`));
      sources(section, point.source_segment_ids);
      $("summary").append(section);
    }
  }
  if (!result.action_items.length) $("actions").append(node("p", "Поручения не зафиксированы."));
  else {
    const wrap = node("div", undefined, "table-wrap"), table = node("table"), head = node("thead"), row = node("tr");
    for (const label of ["№", "Поручение", "Ответственный", "Срок"]) { const th = node("th", label); th.scope = "col"; row.append(th); }
    head.append(row); table.append(head);
    const body = node("tbody");
    result.action_items.forEach((item, index) => {
      const tr = node("tr"), description = node("td");
      description.append(node("p", item.description));
      if (item.condition) description.append(node("p", `Условное поручение: ${item.condition}`));
      for (const milestone of item.milestones) description.append(node("p", `Этап: ${milestone.description}. Срок: ${milestone.deadline_raw ?? "не указан"}`));
      sources(description, item.source_segment_ids);
      const evidence = node("details"); evidence.append(node("summary", "Исходные цитаты"));
      for (const quote of item.evidence) evidence.append(node("blockquote", `[${quote.segment_id}] ${quote.quote}`));
      description.append(evidence);
      tr.append(node("td", index + 1), description, node("td", item.responsible ?? "не указан"), node("td", item.deadline_raw ?? "не указан"));
      body.append(tr);
    });
    table.append(body); wrap.append(table); $("actions").append(wrap);
  }
  if (!result.transcript.length) $("transcript").append(node("p", "Речь не распознана."));
  for (const segment of result.transcript) {
    const section = node("article", undefined, "segment"); section.id = `segment-${segment.id}`; section.tabIndex = -1;
    const speaker = segment.speaker || segment.speaker_ids.join(", ") || "Говорящий не определён";
    section.append(node("div", `[${segment.id}] ${segment.start.toFixed(2)}–${segment.end.toFixed(2)} · ${speaker}`, "muted"), node("p", segment.text));
    $("transcript").append(section);
  }
}
async function readResult() {
  try { renderResult(await api(`/meetings/${meetingId}/result`)); }
  catch (error) { if (error.status !== 409) throw error; }
}
function schedulePoll() {
  clearTimeout(timer);
  if (running) timer = setTimeout(async () => {
    try { await refresh(); } catch (error) { showError(error); schedulePoll(); }
  }, 1200);
}
async function refresh() {
  const meeting = await api(`/meetings/${meetingId}`);
  meetingStatus = meeting.status;
  $("title").value = meeting.title;
  $("meeting-date").value = meeting.meeting_date || "";
  $("meeting-info").hidden = false;
  $("meeting-info").textContent = `Встреча: ${meetingId} · Дата: ${meeting.meeting_date || "не указана"}`;
  const state = await api(`/meetings/${meetingId}/pipeline/status`);
  running = state.status === "running";
  $("downloads").hidden = state.status !== "ready";
  if (state.status === "ready") {
    $("docx").href = `/meetings/${meetingId}/exports/docx`;
    $("pdf").href = `/meetings/${meetingId}/exports/pdf`;
    $("progress").textContent = "Готово. Результат сохранён.";
  } else if (state.status === "failed") {
    $("progress").textContent = `Обработка остановлена: ${stages[state.stage] || state.stage}. Код: ${state.error_code}. Можно повторить обработку.`;
  } else if (running) $("progress").textContent = `${stages[state.stage] || "Обработка"}…`;
  else $("progress").textContent = meetingStatus === "created" ? "Выберите и загрузите запись." : "Запись подготовлена. Нажмите «Обработать».";
  controls();
  if (state.status === "ready") $("analyze").disabled = true;
  if (!running) await readResult();
  schedulePoll();
}
$("upload-form").addEventListener("submit", async (event) => {
  event.preventDefault(); clearError();
  const file = $("file").files[0];
  if (!file || uploading || running) return;
  uploading = true; controls(); $("progress").textContent = "Загрузка и подготовка аудио…";
  try {
    if (!meetingId) {
      const meeting = await api("/meetings", {method: "POST", headers: {"Content-Type": "application/json"},
        body: JSON.stringify({title: $("title").value, meeting_date: $("meeting-date").value || null})});
      meetingId = meeting.id; history.replaceState(null, "", `/#${meetingId}`);
    }
    const data = new FormData(); data.append("file", file);
    await api(`/meetings/${meetingId}/upload`, {method: "POST", body: data});
    await refresh();
  } catch (error) { showError(error); $("progress").textContent = "Загрузка не завершена. Проверьте файл и повторите."; }
  finally { uploading = false; controls(); }
});
$("analyze").addEventListener("click", async () => {
  if (!meetingId || running) return;
  clearError(); running = true; controls(); $("progress").textContent = "Запускаем обработку…"; schedulePoll();
  try { await api(`/meetings/${meetingId}/analyze`, {method: "POST"}); }
  catch (error) { showError(error); }
  finally {
    running = false; clearTimeout(timer);
    try { await refresh(); } catch (error) { showError(error); controls(); }
  }
});
$("refresh").addEventListener("click", async () => { clearError(); try { await refresh(); } catch (error) { showError(error); } });
// Reloading a saved URL only reads state. No automatic POST/inference.
const saved = location.hash.slice(1);
if (/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(saved)) {
  meetingId = saved;
  refresh().catch(showError);
}
