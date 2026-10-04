(() => {
  "use strict";

  const $ = (selector) => document.querySelector(selector);
  const storageKey = "bpmn-assistant-graph-v1";
  const example = "Заявитель подаёт заявку на ремонт оборудования. Диспетчер проверяет полноту данных. Если данных не хватает, он запрашивает уточнение и процесс завершается ожиданием ответа. Если данные полные, служба безопасности проверяет допуск, а инженер оценивает технический риск параллельно. После обеих проверок диспетчер решает, согласовать ремонт или отклонить заявку.";
  const viewer = window.BpmnJS ? new window.BpmnJS({ container: "#canvas" }) : null;
  let graph = null;
  let xml = "";
  let selectedElementId = null;
  let busy = false;

  const message = $("#message");
  const errorBox = $("#error");
  const warningBox = $("#warnings");
  const conversation = $("#conversation");

  function setStatus(text) { $("#status").innerHTML = `<span class="status-dot"></span> ${text}`; }
  function showError(text) { errorBox.textContent = text; errorBox.hidden = false; }
  function clearError() { errorBox.hidden = true; errorBox.textContent = ""; }
  function setBusy(value) {
    busy = value;
    for (const button of [$("#send"), $("#load-demo"), $("#new-diagram")]) button.disabled = value;
    $("#send").textContent = value ? "Создаю схему…" : graph ? "Изменить схему ↗" : "Построить схему ↗";
    setStatus(value ? "Ассистент анализирует процесс…" : "Готов к работе");
  }
  function addMessage(role, text) {
    const bubble = document.createElement("div");
    bubble.className = `message ${role}`;
    const label = document.createElement("span");
    label.className = "message-label";
    label.textContent = role === "user" ? "ВЫ" : "АССИСТЕНТ";
    bubble.appendChild(label);
    bubble.appendChild(document.createTextNode(text));
    conversation.appendChild(bubble);
    conversation.scrollTop = conversation.scrollHeight;
  }
  async function api(url, options) {
    let response;
    try { response = await fetch(url, options); }
    catch { throw new Error("Сервер недоступен. Проверьте, что приложение запущено."); }
    let body;
    try { body = await response.json(); }
    catch { throw new Error(`Сервер вернул некорректный ответ (HTTP ${response.status}).`); }
    if (!response.ok) {
      const detail = body.detail;
      if (typeof detail === "string") throw new Error(detail);
      if (detail && Array.isArray(detail.errors)) throw new Error(detail.errors.join(" "));
      throw new Error(`Ошибка HTTP ${response.status}.`);
    }
    return body;
  }
  async function showDiagram(payload, persist = true) {
    if (!viewer) throw new Error("Не удалось загрузить bpmn-js. Проверьте локальные файлы библиотеки.");
    try { await viewer.importXML(payload.bpmn_xml); }
    catch (error) {
      if (xml) await viewer.importXML(xml);
      throw error;
    }
    viewer.get("canvas").zoom("fit-viewport");
    graph = payload.graph;
    xml = payload.bpmn_xml;
    selectedElementId = null;
    $("#selected-label").textContent = "Нажмите на элемент схемы, чтобы выбрать его для правки";
    $("#empty-state").hidden = true;
    $("#download").disabled = false;
    $("#fit").disabled = false;
    $("#process-name").textContent = graph.process_name;
    $("#diagram-meta").textContent = `${graph.participants.length} участников · ${graph.nodes.length} элементов · ${graph.flows.length} переходов`;
    $("#send").textContent = "Изменить схему ↗";
    warningBox.hidden = !(payload.warnings && payload.warnings.length);
    warningBox.textContent = payload.warnings ? payload.warnings.join(" ") : "";
    if (persist) {
      try { localStorage.setItem(storageKey, JSON.stringify(graph)); }
      catch { showError("Не удалось сохранить схему в браузере. Скачайте .bpmn файл."); }
    }
  }
  async function send() {
    if (busy) return;
    const text = message.value.trim();
    if (!text) { message.focus(); return; }
    clearError();
    setBusy(true);
    try {
      const payload = await api("/api/chat", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ message: text, current_graph: graph || {}, selected_element_id: selectedElementId }),
      });
      await showDiagram(payload);
      addMessage("user", text);
      addMessage("assistant", payload.assistant_message);
      message.value = "";
    } catch (error) { showError(error.message || "Не удалось обновить схему."); }
    finally { setBusy(false); }
  }
  async function loadDemo() {
    if (busy) return;
    clearError(); setBusy(true);
    try {
      const payload = await api("/api/demo");
      await showDiagram(payload);
      addMessage("assistant", payload.assistant_message);
    } catch (error) { showError(error.message); }
    finally { setBusy(false); }
  }
  function reset() {
    if (busy) return;
    graph = null; xml = ""; selectedElementId = null;
    try { localStorage.removeItem(storageKey); } catch { /* storage may be disabled */ }
    if (viewer) viewer.clear();
    $("#empty-state").hidden = false;
    $("#download").disabled = true; $("#fit").disabled = true;
    $("#process-name").textContent = "Новый процесс";
    $("#diagram-meta").textContent = "Схема появится здесь после генерации";
    $("#selected-label").textContent = "Нажмите на элемент схемы, чтобы выбрать его для правки";
    $("#send").textContent = "Построить схему ↗";
    warningBox.hidden = true; conversation.innerHTML = "";
    clearError(); setStatus("Готов к работе");
    message.focus();
  }
  function download() {
    if (!xml) return;
    const blob = new Blob([xml], { type: "application/xml;charset=utf-8" });
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = url; link.download = "process.bpmn";
    document.body.appendChild(link); link.click(); link.remove();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  }
  function registerSelection() {
    if (!viewer) return;
    const eventBus = viewer.get("eventBus");
    const canvas = viewer.get("canvas");
    eventBus.on("element.click", (event) => {
      const element = event.element;
      if (!graph || !element) return;
      const found = [...graph.nodes, ...graph.participants, ...graph.flows].find((item) => item.id === element.id);
      if (!found) return;
      if (selectedElementId) canvas.removeMarker(selectedElementId, "selected-by-chat");
      selectedElementId = found.id;
      canvas.addMarker(selectedElementId, "selected-by-chat");
      $("#selected-label").textContent = `Выбрано: ${found.name || found.type || found.id}`;
    });
  }
  async function restore() {
    try {
      const saved = localStorage.getItem(storageKey);
      if (!saved) return;
      const payload = await api("/api/render", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ graph: JSON.parse(saved) }),
      });
      await showDiagram(payload, false);
      addMessage("assistant", "Восстановлена схема из этого браузера. Продолжайте редактирование.");
    } catch {
      try { localStorage.removeItem(storageKey); } catch { /* ignore */ }
      showError("Не удалось восстановить сохранённую схему. Создайте новую или откройте демо.");
    }
  }

  $("#send").addEventListener("click", send);
  message.addEventListener("keydown", (event) => { if (event.key === "Enter" && (event.ctrlKey || event.metaKey)) { event.preventDefault(); send(); } });
  $("#fill-example").addEventListener("click", () => { message.value = example; message.focus(); });
  $("#load-demo").addEventListener("click", loadDemo);
  $("#new-diagram").addEventListener("click", reset);
  $("#fit").addEventListener("click", () => { if (viewer) viewer.get("canvas").zoom("fit-viewport"); });
  $("#download").addEventListener("click", download);
  registerSelection();
  restore();
})();
