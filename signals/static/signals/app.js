(() => {
  "use strict";

  const state = {
    token: null,
    channelNames: [],
    markers: [],
    isEyesTest: false,
    artifactRanges: [],
    markingArtifact: false,
    fs: null,
    duration: null,
    viewMode: "grid", // "grid" | "focus" | "bands" | "compare"
    focusChannel: null,
    lastData: null,
    lastChannels: [],
    lastBandsData: null,
    lastBandsCompareData: null,
    testsList: [],
    selectedTests: new Set(),
    lastCompareData: null,
    compareFocusChannel: null,
  };

  const BAND_COLORS = { theta: "#a78bfa", alpha: "#38bdf8", beta: "#fbbf24", gamma: "#fb7185" };
  const BAND_LABELS = { theta: "Teta", alpha: "Alfa", beta: "Beta", gamma: "Gama" };
  const TEST_COLORS = ["#5aa9ff", "#facc15", "#4ade80", "#fb7185", "#a78bfa", "#38bdf8", "#f97316", "#f472b6"];
  const testColor = (i) => TEST_COLORS[i % TEST_COLORS.length];

  // Lab protocol: 5 marker events bound 4 alternating eyes-closed/eyes-open
  // segments (closed1, open1, closed2, open2) -- same convention as
  // dsp.band_power_by_markers on the server. When a recording has that many
  // markers we show exactly which stretch of the signal is which condition,
  // instead of just an unlabeled "[Marker]" tick at each event.
  // Only applied when the recording was explicitly flagged as an eyes test
  // at upload time (state.isEyesTest) -- other protocols will be recorded
  // with this same tool and could coincidentally also have 5 markers.
  function conditionSegments(markers) {
    if (!state.isEyesTest || !markers || markers.length < 5) return null;
    const times = markers.map((m) => m.sample_time).slice(0, 5).sort((a, b) => a - b);
    const labels = ["Olhos fechados", "Olhos abertos", "Olhos fechados", "Olhos abertos"];
    return labels.map((label, i) => ({ t0: times[i], t1: times[i + 1], label, closed: i % 2 === 0 }));
  }

  // Returns {shapes, annotations} for a Plotly layout: shaded closed/open
  // bands with a label at the exact boundary when the protocol is
  // recognized (marked as an eyes test, >=5 markers), otherwise the older
  // generic marker ticks.
  function markerConditionLayout(markers, { fontSize = 10 } = {}) {
    const segments = conditionSegments(markers);
    if (!segments) {
      return {
        shapes: (markers || []).map((m) => ({
          type: "line", xref: "x", yref: "paper", x0: m.sample_time, x1: m.sample_time,
          y0: 0, y1: 1, line: { color: "#4ade80", width: 1, dash: "dot" },
        })),
        annotations: (markers || []).map((m) => ({
          x: m.sample_time, y: 1, yref: "paper", xref: "x",
          text: m.tag, showarrow: false, font: { size: fontSize, color: "#4ade80" }, yshift: 4,
        })),
      };
    }
    const shapes = [];
    const annotations = [];
    segments.forEach((seg) => {
      const color = seg.closed ? "#5aa9ff" : "#facc15";
      shapes.push({
        type: "rect", xref: "x", yref: "paper", x0: seg.t0, x1: seg.t1, y0: 0, y1: 1,
        fillcolor: seg.closed ? "rgba(90,169,255,0.08)" : "rgba(250,204,21,0.08)",
        line: { width: 0 }, layer: "below",
      });
      shapes.push({
        type: "line", xref: "x", yref: "paper", x0: seg.t0, x1: seg.t0, y0: 0, y1: 1,
        line: { color, width: 1.5, dash: "dot" },
      });
      annotations.push({
        x: (seg.t0 + seg.t1) / 2, y: 1, yref: "paper", xref: "x",
        text: seg.label, showarrow: false, font: { size: fontSize, color }, yshift: 4,
      });
    });
    const last = segments[segments.length - 1];
    shapes.push({
      type: "line", xref: "x", yref: "paper", x0: last.t1, x1: last.t1, y0: 0, y1: 1,
      line: { color: "#94a3b8", width: 1, dash: "dot" },
    });
    return { shapes, annotations };
  }

  // Manually-marked artifact ranges (blinks, jaw clench, etc.) -- shaded red,
  // full-height, tagged `_kind: "system"` so the plotly_relayout handler can
  // tell them apart from a rectangle the user just drew. Applies to every
  // channel (an artifact like a blink shows on all of them), independent of
  // channel selection.
  function artifactShapes(ranges) {
    return (ranges || []).map(([t0, t1]) => ({
      type: "rect", xref: "x", yref: "paper", x0: t0, x1: t1, y0: 0, y1: 1,
      fillcolor: "rgba(239,68,68,0.15)", line: { color: "#ef4444", width: 1 },
      layer: "above", _kind: "system",
    }));
  }

  const el = (id) => document.getElementById(id);

  function getCookie(name) {
    const match = document.cookie.match(new RegExp("(^| )" + name + "=([^;]+)"));
    return match ? decodeURIComponent(match[2]) : null;
  }
  const csrftoken = () => getCookie("csrftoken");

  // The session cookie can expire while the tab is open (long recording
  // review, laptop asleep overnight); every API call ends up here on a 401,
  // so send the user back to the login page instead of showing a confusing
  // "unexpected error" from whatever fetch happened to be in flight.
  function redirectToLoginIfExpired(resp) {
    if (resp.status === 401) {
      window.location.href = "/login/?next=" + encodeURIComponent(window.location.pathname);
      return true;
    }
    return false;
  }

  async function postJSON(url, body) {
    const resp = await fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-CSRFToken": csrftoken() },
      body: JSON.stringify(body),
    });
    if (redirectToLoginIfExpired(resp)) return new Promise(() => {}); // navigating away
    const data = await resp.json().catch(() => ({ ok: false, error: "Resposta inválida do servidor." }));
    if (!resp.ok || !data.ok) throw new Error(data.error || `Erro HTTP ${resp.status}`);
    return data;
  }

  async function postFile(url, file, extra) {
    const form = new FormData();
    form.append("file", file);
    Object.entries(extra || {}).forEach(([k, v]) => form.append(k, v));
    const resp = await fetch(url, { method: "POST", headers: { "X-CSRFToken": csrftoken() }, body: form });
    if (redirectToLoginIfExpired(resp)) return new Promise(() => {}); // navigating away
    const data = await resp.json().catch(() => ({ ok: false, error: "Resposta inválida do servidor." }));
    if (!resp.ok || !data.ok) throw new Error(data.error || `Erro HTTP ${resp.status}`);
    return data;
  }

  function showError(target, message) {
    const box = el(target);
    box.textContent = message;
    box.classList.toggle("hidden", !message);
  }

  // ---- upload ----------------------------------------------------------

  function setupUpload() {
    const dropzone = el("dropzone");
    const input = el("file-input");

    dropzone.addEventListener("click", () => input.click());
    dropzone.addEventListener("dragover", (e) => { e.preventDefault(); dropzone.classList.add("dragover"); });
    dropzone.addEventListener("dragleave", () => dropzone.classList.remove("dragover"));
    dropzone.addEventListener("drop", (e) => {
      e.preventDefault();
      dropzone.classList.remove("dragover");
      if (e.dataTransfer.files.length) handleFile(e.dataTransfer.files[0]);
    });
    input.addEventListener("change", () => {
      if (input.files.length) handleFile(input.files[0]);
    });
  }

  async function handleFile(file) {
    showError("upload-error", "");
    el("dropzone-label").textContent = `Processando ${file.name}…`;
    try {
      const data = await postFile("/api/upload/", file, {
        name: el("upload-name").value,
        participant_name: el("upload-participant").value,
        sex: el("upload-sex").value,
        age: el("upload-age").value,
        is_eyes_test: el("upload-is-eyes-test").checked ? "1" : "0",
      });
      state.token = data.token;
      state.channelNames = data.channel_names;
      state.markers = data.markers || [];
      state.isEyesTest = !!data.is_eyes_test;
      state.fs = data.fs;
      state.duration = data.duration_s;
      state.lastBandsData = null;
      state.lastBandsCompareData = null;
      state.artifactRanges = [];
      setArtifactMarking(false);
      renderArtifactsBar();
      el("mark-artifact-btn").classList.remove("hidden");
      fetchTestsList();

      el("dropzone-label").textContent = `${file.name} — ${data.n_samples} amostras`;
      el("info-fs").textContent = data.fs.toFixed(2);
      el("info-n").textContent = data.n_samples;
      el("info-dur").textContent = data.duration_s.toFixed(2);
      el("info-markers").textContent = state.markers.length;
      el("dataset-info").classList.remove("hidden");
      el("saturation-threshold").value = Math.round(data.default_saturation_uv * 0.997);

      buildChannelList();
      el("controls-section").classList.remove("disabled");
      el("plot-placeholder").classList.add("hidden");
      await applyFilters();
    } catch (err) {
      showError("upload-error", err.message);
      el("dropzone-label").textContent = "Arraste o CSV do OpenBCI aqui ou clique para escolher";
    }
  }

  function buildChannelList() {
    const list = el("channel-list");
    list.innerHTML = "";
    state.channelNames.forEach((name, i) => {
      const label = document.createElement("label");
      const cb = document.createElement("input");
      cb.type = "checkbox";
      cb.checked = i < 8; // all channels by default
      cb.value = name;
      cb.className = "channel-cb";
      cb.addEventListener("change", () => { scheduleApply(); });
      const badge = document.createElement("span");
      badge.className = "sat-badge";
      badge.id = `sat-badge-${name}`;
      badge.textContent = "—";
      label.appendChild(cb);
      label.appendChild(document.createTextNode(name));
      label.appendChild(badge);
      list.appendChild(label);
    });
  }

  function updateSaturationBadges(data) {
    Object.entries(data.channels).forEach(([name, ch]) => {
      const badge = el(`sat-badge-${name}`);
      if (!badge) return;
      const pct = ch.saturation_pct;
      badge.textContent = `${pct.toFixed(1)}%`;
      badge.classList.remove("ok", "warn", "bad");
      badge.classList.add(pct >= 20 ? "bad" : pct >= 5 ? "warn" : "ok");
    });
  }

  const RISK_THRESHOLD_PCT = 20;

  function updateQualityBanner(data, channels) {
    const banner = el("quality-banner");
    const risky = channels
      .map((name) => ({ name, ...data.channels[name] }))
      .filter((ch) => ch.saturation_pct >= RISK_THRESHOLD_PCT);
    if (!risky.length) {
      banner.classList.add("hidden");
      banner.innerHTML = "";
      return;
    }
    const list = risky
      .map((ch) => `<b>${ch.name}</b> (${ch.saturation_pct.toFixed(1)}% saturado)`)
      .join(", ");
    banner.innerHTML = `⚠ Saturação alta em ${list} — provavelmente eletrodo mal posicionado ou impedância alta. A interpolação evita que o filtro "quebre", mas o trecho ainda fica marcado como descartado (cinza) no gráfico; considere refazer a coleta ou excluir esse(s) canal(is) da análise.`;
    banner.classList.remove("hidden");
  }

  function selectedChannels() {
    return Array.from(document.querySelectorAll(".channel-cb:checked")).map((cb) => cb.value);
  }

  // ---- filter params -----------------------------------------------------

  function currentParams() {
    return {
      detrend: {
        enabled: el("detrend-enabled").checked,
        kind: el("detrend-kind").value,
      },
      bandpass: {
        enabled: el("bandpass-enabled").checked,
        low: parseFloat(el("bandpass-low").value),
        high: parseFloat(el("bandpass-high").value),
        order: parseInt(el("bandpass-order").value, 10),
      },
      notch: {
        enabled: el("notch-enabled").checked,
        freq: parseFloat(el("notch-freq").value),
        q: parseFloat(el("notch-q").value),
        harmonics: parseInt(el("notch-harmonics").value, 10),
      },
      saturation: {
        enabled: el("saturation-enabled").checked,
        threshold: parseFloat(el("saturation-threshold").value),
        interpolate: el("interpolate-enabled").checked,
        max_gap_ms: parseFloat(el("max-gap-ms").value),
      },
    };
  }

  let applyTimer = null;
  function scheduleApply() {
    if (!state.token) return;
    clearTimeout(applyTimer);
    applyTimer = setTimeout(applyFilters, 350);
  }

  function setupControlListeners() {
    document.querySelectorAll('#controls-section input, #controls-section select').forEach((elm) => {
      const evt = elm.type === "range" || elm.type === "number" ? "input" : "change";
      elm.addEventListener(evt, scheduleApply);
    });
    el("bandpass-order").addEventListener("input", () => {
      el("bandpass-order-val").textContent = el("bandpass-order").value;
    });
    el("notch-q").addEventListener("input", () => {
      el("notch-q-val").textContent = el("notch-q").value;
    });
    el("apply-btn").addEventListener("click", applyFilters);
    el("download-btn").addEventListener("click", downloadFiltered);
    el("download-bands-btn").addEventListener("click", downloadBands);
  }

  // ---- apply / plot -----------------------------------------------------

  async function applyFilters() {
    if (!state.token) return;
    showError("process-error", "");
    const channels = selectedChannels();
    if (!channels.length) {
      showError("process-error", "Selecione ao menos um canal.");
      return;
    }
    el("apply-btn").disabled = true;
    try {
      const data = await postJSON("/api/process/", {
        token: state.token,
        channels,
        params: currentParams(),
      });
      state.lastData = data;
      state.lastChannels = channels;
      state.artifactRanges = data.artifact_ranges || [];
      renderArtifactsBar();
      if (!channels.includes(state.focusChannel)) state.focusChannel = channels[0];
      updateSaturationBadges(data);
      updateQualityBanner(data, channels);
      el("plot-toolbar").classList.remove("hidden");
      if (state.viewMode === "bands") {
        await Promise.all([fetchBands(), fetchBandsCompare()]);
      }
      renderAll(data, channels);
    } catch (err) {
      showError("process-error", err.message);
    } finally {
      el("apply-btn").disabled = false;
    }
  }

  async function fetchBands() {
    if (!state.token || !state.lastChannels.length) return;
    try {
      state.lastBandsData = await postJSON("/api/bands/", {
        token: state.token,
        channels: state.lastChannels,
        params: currentParams(),
      });
    } catch (err) {
      showError("process-error", err.message);
    }
  }

  async function fetchBandsCompare() {
    if (!state.token || !state.lastChannels.length) return;
    try {
      state.lastBandsCompareData = await postJSON("/api/bands/compare/", {
        token: state.token,
        channels: state.lastChannels,
        params: currentParams(),
      });
    } catch (err) {
      showError("process-error", err.message);
    }
  }

  // ---- multi-test registry & comparison ---------------------------------
  // Every upload is kept on the server (named, with a date) so a student can
  // come back later and compare any two (or more) registered recordings --
  // this is a *between-recordings* comparison (whole signal, no marker
  // segmentation), distinct from the within-recording closed/open comparison
  // above, which needs the 5-marker eyes-closed/eyes-open protocol.

  async function fetchTestsList() {
    try {
      const resp = await fetch("/api/tests/");
      if (redirectToLoginIfExpired(resp)) return;
      const data = await resp.json().catch(() => ({ ok: false, error: "Resposta inválida do servidor." }));
      if (!resp.ok || !data.ok) throw new Error(data.error || `Erro HTTP ${resp.status}`);
      state.testsList = data.tests;
      for (const token of Array.from(state.selectedTests)) {
        if (!state.testsList.some((t) => t.token === token)) state.selectedTests.delete(token);
      }
      renderTestsList();
    } catch (err) {
      showError("tests-error", err.message);
    }
  }

  function renderTestsList() {
    const container = el("tests-list");
    container.innerHTML = "";
    state.testsList.forEach((t) => {
      const row = document.createElement("div");
      row.className = "test-item";

      const label = document.createElement("label");
      const cb = document.createElement("input");
      cb.type = "checkbox";
      cb.checked = state.selectedTests.has(t.token);
      cb.addEventListener("change", () => {
        if (cb.checked) state.selectedTests.add(t.token);
        else state.selectedTests.delete(t.token);
        el("tests-compare-btn").disabled = state.selectedTests.size < 2;
      });
      const nameSpan = document.createElement("span");
      nameSpan.className = "test-name";
      nameSpan.textContent = t.name;
      const detailLines = [t.name];
      if (t.participant_name) detailLines.push(`Pessoa: ${t.participant_name}`);
      const sexAge = [t.sex === "F" ? "Feminino" : t.sex === "M" ? "Masculino" : t.sex === "outro" ? "Outro" : null, t.age ? `${t.age} anos` : null]
        .filter(Boolean).join(", ");
      if (sexAge) detailLines.push(sexAge);
      detailLines.push(new Date(t.uploaded_at).toLocaleString("pt-BR"));
      nameSpan.title = detailLines.join("\n");
      label.appendChild(cb);
      label.appendChild(nameSpan);
      if (t.is_eyes_test) {
        const eyeBadge = document.createElement("span");
        eyeBadge.className = "eye-badge";
        eyeBadge.textContent = "👁";
        eyeBadge.title = "Marcado como teste de olhos fechados/abertos";
        label.appendChild(eyeBadge);
      }

      const meta = document.createElement("span");
      meta.className = "test-meta";
      meta.textContent = `${Math.round(t.duration_s)}s`;
      meta.title = nameSpan.title;

      const delBtn = document.createElement("button");
      delBtn.type = "button";
      delBtn.className = "test-delete";
      delBtn.textContent = "✕";
      delBtn.title = "Remover teste cadastrado";
      delBtn.addEventListener("click", (e) => {
        e.preventDefault();
        if (!window.confirm(`Remover "${t.name}" da lista de testes?`)) return;
        deleteTest(t.token);
      });

      row.appendChild(label);
      row.appendChild(meta);
      row.appendChild(delBtn);
      container.appendChild(row);
    });
    el("tests-compare-btn").disabled = state.selectedTests.size < 2;
  }

  async function deleteTest(token) {
    try {
      await postJSON("/api/tests/delete/", { token });
      state.selectedTests.delete(token);
      if (state.lastCompareData) {
        state.lastCompareData.tests = state.lastCompareData.tests.filter((t) => t.token !== token);
      }
      await fetchTestsList();
    } catch (err) {
      showError("tests-error", err.message);
    }
  }

  async function runCompare() {
    const tokens = Array.from(state.selectedTests);
    if (tokens.length < 2) return;
    showError("tests-error", "");
    el("tests-compare-btn").disabled = true;
    try {
      state.lastCompareData = await postJSON("/api/compare/", {
        tokens,
        channels: state.lastChannels.length ? state.lastChannels : null,
        params: currentParams(),
      });
      state.compareFocusChannel = null;
      el("plot-toolbar").classList.remove("hidden");
      el("plot-placeholder").classList.add("hidden");
      setViewMode("compare");
    } catch (err) {
      showError("tests-error", err.message);
    } finally {
      el("tests-compare-btn").disabled = state.selectedTests.size < 2;
    }
  }

  function renderCompareAll() {
    const data = state.lastCompareData;
    const hint = el("compare-hint");
    if (!data) {
      hint.textContent = "Selecione 2 ou mais testes cadastrados na barra lateral (seção 4) e clique em \"Comparar selecionados\".";
      hint.classList.remove("hidden");
      el("compare-bar-plot").classList.add("hidden");
      el("compare-signal-plot").classList.add("hidden");
      return;
    }
    hint.classList.add("hidden");
    el("compare-bar-plot").classList.remove("hidden");
    el("compare-signal-plot").classList.remove("hidden");

    if (!state.compareFocusChannel || !data.channel_names.includes(state.compareFocusChannel)) {
      state.compareFocusChannel = data.channel_names[0];
    }
    buildFocusChannelSelect(data.channel_names, state.compareFocusChannel);
    el("compare-channel").textContent = state.compareFocusChannel;
    el("compare-channel-2").textContent = state.compareFocusChannel;

    const legend = el("compare-legend");
    legend.innerHTML = "";
    data.tests.forEach((t, i) => {
      const item = document.createElement("span");
      item.className = "legend-item";
      const swatch = document.createElement("i");
      swatch.className = "swatch";
      swatch.style.background = testColor(i);
      const label = document.createElement("span");
      label.textContent = t.name;
      item.appendChild(swatch);
      item.appendChild(label);
      legend.appendChild(item);
    });

    renderCompareBar(data, state.compareFocusChannel);
    renderCompareSignal(data, state.compareFocusChannel);
  }

  function renderCompareBar(data, channelName) {
    const labels = data.band_names.map((b) => BAND_LABELS[b]);
    const traces = data.tests.map((t, i) => ({
      x: labels,
      y: data.band_names.map((b) => data.band_power[t.token]?.[channelName]?.[b] ?? null),
      type: "bar", name: t.name,
      marker: { color: testColor(i) },
      hovertemplate: "%{y:.1f} µV<extra>" + t.name + "</extra>",
    }));
    const layout = {
      barmode: "group",
      showlegend: false,
      margin: { t: 20, r: 20, b: 40, l: 60 },
      paper_bgcolor: "#171d2c", plot_bgcolor: "#171d2c",
      font: { color: "#e6e9f2", size: 11 },
      yaxis: { title: { text: "Potência RMS (µV)" }, gridcolor: "#242c40", zeroline: false },
      xaxis: { gridcolor: "#242c40" },
    };
    Plotly.react(el("compare-bar-plot"), traces, layout, { responsive: true, displaylogo: false });
  }

  function renderCompareSignal(data, channelName) {
    const traces = data.tests.map((t, i) => {
      const s = data.series[t.token];
      return {
        x: s ? s.t : [], y: (s && s.channels[channelName]) || [],
        type: "scattergl", mode: "lines", name: t.name,
        line: { color: testColor(i), width: 1.3 },
        hovertemplate: "%{y:.1f} µV<extra>" + t.name + "</extra>",
      };
    });
    // Exact eyes-closed/open marker points for each test, in that test's own
    // color -- ticks only (no shaded segments) since two tests rarely share
    // marker times and stacked shading would just clutter the overlay.
    const shapes = [];
    data.tests.forEach((t, i) => {
      const testMarkers = (data.markers && data.markers[t.token]) || [];
      testMarkers.forEach((m) => {
        shapes.push({
          type: "line", xref: "x", yref: "paper", x0: m.sample_time, x1: m.sample_time,
          y0: 0, y1: 1, line: { color: testColor(i), width: 1.2, dash: "dot" },
        });
      });
      const testArtifacts = (data.artifact_ranges && data.artifact_ranges[t.token]) || [];
      testArtifacts.forEach(([t0, t1]) => {
        shapes.push({
          type: "rect", xref: "x", yref: "paper", x0: t0, x1: t1, y0: 0, y1: 1,
          fillcolor: "rgba(239,68,68,0.12)", line: { color: "#ef4444", width: 1 }, layer: "above",
        });
      });
    });
    const layout = {
      showlegend: false,
      margin: { t: 20, r: 20, b: 40, l: 60 },
      paper_bgcolor: "#171d2c", plot_bgcolor: "#171d2c",
      font: { color: "#e6e9f2", size: 11 },
      hovermode: "x unified",
      shapes,
      xaxis: { title: { text: "Tempo (s)" }, gridcolor: "#242c40" },
      yaxis: {
        title: { text: "Amplitude filtrada (µV)" },
        gridcolor: "#242c40", zeroline: true, zerolinecolor: "#3a4460",
      },
    };
    Plotly.react(el("compare-signal-plot"), traces, layout, { responsive: true, displaylogo: false });
  }

  function setupTestsControls() {
    el("tests-refresh-btn").addEventListener("click", fetchTestsList);
    el("tests-compare-btn").addEventListener("click", runCompare);
  }

  function renderAll(data, channels) {
    if (state.viewMode === "compare") return; // compare mode has its own render path (renderCompareAll)
    buildFocusChannelSelect(channels, state.focusChannel);
    if (state.viewMode === "grid") {
      renderGrid(data, channels);
    } else if (state.viewMode === "focus") {
      renderFocus(data, state.focusChannel);
    } else if (state.viewMode === "bands") {
      if (state.lastBandsData) renderBands(state.lastBandsData, state.focusChannel);
      if (state.lastBandsCompareData) renderBandsCompare(state.lastBandsCompareData, state.focusChannel);
    }
    const fullRange = [data.t[0], data.t[data.t.length - 1]];
    renderTimeline(data, [state.focusChannel || channels[0]], fullRange);
  }

  function renderGrid(data, channels) {
    const t = data.t;
    const n = channels.length;
    const traces = [];
    const layoutShapes = [];
    const annotations = [];

    channels.forEach((name, i) => {
      const axisNum = i + 1;
      const xaxis = "x";
      const yaxis = axisNum === 1 ? "y" : `y${axisNum}`;
      const ch = data.channels[name];

      traces.push({
        x: t, y: ch.raw, type: "scattergl", mode: "lines",
        name: `${name} bruto`, legendgroup: name, xaxis, yaxis,
        line: { color: "#5a6480", width: 1 }, opacity: 0.55,
        hovertemplate: "%{y:.1f} µV<extra>" + name + " bruto</extra>",
      });
      traces.push({
        x: t, y: ch.filtered, type: "scattergl", mode: "lines",
        name: `${name} filtrado`, legendgroup: name, xaxis, yaxis,
        line: { color: "#5aa9ff", width: 1.4 },
        hovertemplate: "%{y:.1f} µV<extra>" + name + " filtrado</extra>",
      });
      if (ch.saturation_t.length) {
        traces.push({
          x: ch.saturation_t,
          y: ch.saturation_t.map(() => 0),
          type: "scattergl", mode: "markers", name: `${name} saturação`,
          legendgroup: name, xaxis, yaxis, showlegend: false,
          marker: { color: "#ff6b6b", size: 5, symbol: "line-ns-open" },
          hovertemplate: "saturado<extra>" + name + "</extra>",
        });
      }
      (ch.excluded_ranges || []).forEach(([t0, t1]) => {
        layoutShapes.push({
          type: "rect", xref: "x", yref: `${yaxis} domain`,
          x0: t0, x1: t1, y0: 0, y1: 1,
          fillcolor: "rgba(148,163,184,0.55)", line: { width: 0 }, layer: "above",
        });
      });
    });

    const markerLayout = markerConditionLayout(state.markers, { fontSize: 9 });
    layoutShapes.push(...markerLayout.shapes);
    annotations.push(...markerLayout.annotations);
    layoutShapes.push(...artifactShapes(state.artifactRanges));
    layoutShapes.forEach((s) => { s._kind = "system"; });

    const fullRange = [t[0], t[t.length - 1]];
    const satColor = (pct) => (pct >= 20 ? "#ff6b6b" : pct >= 5 ? "#facc15" : "#9aa4bd");

    const layout = {
      grid: { rows: n, columns: 1, pattern: "independent", roworder: "top to bottom" },
      showlegend: false,
      margin: { t: 20, r: 20, b: 40, l: 70 },
      paper_bgcolor: "#171d2c",
      plot_bgcolor: "#171d2c",
      font: { color: "#e6e9f2", size: 11 },
      shapes: layoutShapes,
      annotations,
      hovermode: "x unified",
      dragmode: state.markingArtifact ? "drawrect" : "zoom",
      newshape: { fillcolor: "rgba(239,68,68,0.2)", line: { color: "#ef4444", width: 1.5 } },
    };
    channels.forEach((name, i) => {
      const axisNum = i + 1;
      const yKey = axisNum === 1 ? "yaxis" : `yaxis${axisNum}`;
      const xKey = axisNum === 1 ? "xaxis" : `xaxis${axisNum}`;
      const pct = data.channels[name].saturation_pct;
      layout[yKey] = {
        title: { text: `<b>${name}</b><br>${pct.toFixed(1)}% sat.`, font: { size: 10, color: satColor(pct) } },
        zeroline: false,
        gridcolor: "#242c40",
        showticklabels: true,
        ticks: "outside", ticklen: 3, tickfont: { size: 8 },
      };
      layout[xKey] = {
        matches: "x",
        showticklabels: axisNum === n,
        title: axisNum === n ? { text: "Tempo (s)" } : undefined,
        range: axisNum === 1 ? fullRange : undefined,
        gridcolor: "#242c40",
      };
    });

    const plotDiv = el("plot");
    plotDiv.style.height = `${Math.max(600, n * 160)}px`;
    Plotly.react(plotDiv, traces, layout, { responsive: true, displaylogo: false });
    wireArtifactDrawing(plotDiv);
  }

  function buildFocusChannelSelect(channels, preferred) {
    const select = el("focus-channel-select");
    const current = select.value;
    select.innerHTML = "";
    channels.forEach((name) => {
      const opt = document.createElement("option");
      opt.value = name;
      opt.textContent = name;
      select.appendChild(opt);
    });
    // An explicit `preferred` reflects authoritative state (state.focusChannel
    // or state.compareFocusChannel) and wins over whatever the dropdown
    // happened to show before -- that stale value can belong to the *other*
    // channel-state (e.g. left over from "Comparar testes" mode).
    if (preferred !== undefined && channels.includes(preferred)) {
      select.value = preferred;
    } else {
      select.value = channels.includes(current) ? current : state.focusChannel;
    }
  }

  function activeChannelList() {
    return state.viewMode === "compare" && state.lastCompareData
      ? state.lastCompareData.channel_names
      : state.lastChannels;
  }

  function renderFocus(data, channelName) {
    if (!channelName || !data.channels[channelName]) return;
    const t = data.t;
    const ch = data.channels[channelName];

    const badge = el("focus-sat-badge");
    const pct = ch.saturation_pct;
    badge.textContent = `${channelName}: ${pct.toFixed(1)}% saturado`;
    badge.classList.remove("ok", "warn", "bad");
    badge.classList.add(pct >= 20 ? "bad" : pct >= 5 ? "warn" : "ok");

    const traces = [
      {
        x: t, y: ch.raw, type: "scattergl", mode: "lines", name: "Sinal bruto",
        line: { color: "#5a6480", width: 1 }, opacity: 0.6,
        hovertemplate: "%{y:.1f} µV<extra>bruto</extra>",
      },
      {
        x: t, y: ch.filtered, type: "scattergl", mode: "lines", name: "Sinal filtrado",
        line: { color: "#5aa9ff", width: 1.8 },
        hovertemplate: "%{y:.1f} µV<extra>filtrado</extra>",
      },
    ];
    if (ch.saturation_t.length) {
      traces.push({
        x: ch.saturation_t, y: ch.saturation_t.map(() => 0),
        type: "scattergl", mode: "markers", name: "Saturação",
        marker: { color: "#ff6b6b", size: 8, symbol: "line-ns-open", line: { width: 2 } },
        hovertemplate: "saturado<extra></extra>",
      });
    }

    const shapes = (ch.excluded_ranges || []).map(([t0, t1]) => ({
      type: "rect", xref: "x", yref: "y domain",
      x0: t0, x1: t1, y0: 0, y1: 1,
      fillcolor: "rgba(148,163,184,0.55)", line: { width: 0 }, layer: "above",
    }));
    const markerLayout = markerConditionLayout(state.markers, { fontSize: 11 });
    shapes.push(...markerLayout.shapes);
    shapes.push(...artifactShapes(state.artifactRanges));
    shapes.forEach((s) => { s._kind = "system"; });
    const annotations = markerLayout.annotations;

    const layout = {
      margin: { t: 30, r: 30, b: 50, l: 70 },
      paper_bgcolor: "#171d2c",
      plot_bgcolor: "#171d2c",
      font: { color: "#e6e9f2", size: 12 },
      legend: { orientation: "h", y: 1.08, font: { size: 11 } },
      shapes,
      annotations,
      hovermode: "x unified",
      dragmode: state.markingArtifact ? "drawrect" : "zoom",
      newshape: { fillcolor: "rgba(239,68,68,0.2)", line: { color: "#ef4444", width: 1.5 } },
      xaxis: {
        title: { text: "Tempo (s)" },
        range: [t[0], t[t.length - 1]],
        gridcolor: "#242c40",
      },
      yaxis: {
        title: { text: "Amplitude (µV)" },
        gridcolor: "#242c40",
        zeroline: true,
        zerolinecolor: "#3a4460",
      },
    };

    const focusDiv = el("focus-plot");
    Plotly.react(focusDiv, traces, layout, { responsive: true, displaylogo: false });
    wireArtifactDrawing(focusDiv);
  }

  function renderBands(data, channelName) {
    if (!channelName || !data.channels[channelName]) return;
    const t = data.t;
    const ch = data.channels[channelName];
    const bandNames = data.band_names;
    const n = bandNames.length;

    const badge = el("focus-sat-badge");
    const pct = ch.saturation_pct;
    badge.textContent = `${channelName}: ${pct.toFixed(1)}% saturado`;
    badge.classList.remove("ok", "warn", "bad");
    badge.classList.add(pct >= 20 ? "bad" : pct >= 5 ? "warn" : "ok");

    const traces = [];
    const fullRange = [t[0], t[t.length - 1]];
    const markerLayout = markerConditionLayout(state.markers, { fontSize: 9 });
    const bandsShapes = [...markerLayout.shapes, ...artifactShapes(state.artifactRanges)];
    bandsShapes.forEach((s) => { s._kind = "system"; });
    const layout = {
      showlegend: false,
      margin: { t: 20, r: 20, b: 40, l: 70 },
      paper_bgcolor: "#171d2c",
      plot_bgcolor: "#171d2c",
      font: { color: "#e6e9f2", size: 11 },
      hovermode: "x unified",
      shapes: bandsShapes,
      annotations: markerLayout.annotations,
    };

    // Plotly's layout.grid auto-domain computation (pattern:"independent")
    // only assigns a real domain to the first axis and leaves the rest at
    // the full-height default [0,1] (reproduced on plain minimal traces,
    // unrelated to anything else in this page) -- so rows are laid out
    // with explicit domains instead of relying on that mechanism.
    const rowGap = 0.06;
    const rowHeight = (1 - rowGap * (n - 1)) / n;

    bandNames.forEach((band, i) => {
      const axisNum = i + 1;
      const suffix = axisNum === 1 ? "" : String(axisNum);
      const xaxis = "x" + suffix;
      const yaxis = "y" + suffix;
      const range = data.band_ranges[band];
      const power = ch.band_power_rms_uv[band];
      traces.push({
        x: t, y: ch.bands[band], type: "scattergl", mode: "lines", xaxis, yaxis,
        line: { color: BAND_COLORS[band], width: 1.3 },
        hovertemplate: "%{y:.1f} µV<extra>" + BAND_LABELS[band] + "</extra>",
      });
      const top = 1 - i * (rowHeight + rowGap);
      layout["yaxis" + suffix] = {
        domain: [top - rowHeight, top],
        anchor: xaxis,
        title: {
          text: `<b>${BAND_LABELS[band]}</b><br>${range[0]}–${range[1]} Hz<br>RMS ${power} µV`,
          font: { size: 10, color: BAND_COLORS[band] },
        },
        zeroline: false, gridcolor: "#242c40",
      };
      layout["xaxis" + suffix] = {
        domain: [0, 1],
        anchor: yaxis,
        matches: axisNum === 1 ? undefined : "x",
        showticklabels: axisNum === n,
        title: axisNum === n ? { text: "Tempo (s)" } : undefined,
        range: axisNum === 1 ? fullRange : undefined,
        gridcolor: "#242c40",
      };
    });

    const plotDiv = el("bands-plot");
    plotDiv.style.height = `${Math.max(600, n * 160)}px`;
    Plotly.react(plotDiv, traces, layout, { responsive: true, displaylogo: false });
  }

  function renderBandsCompare(data, channelName) {
    const wrap = el("bands-compare-wrap");
    const hint = el("bands-compare-hint");
    el("bands-compare-channel").textContent = channelName || "";

    if (!data.available) {
      el("bands-compare-plot").classList.add("hidden");
      hint.textContent = data.reason || "Comparação indisponível para esta gravação.";
      hint.classList.remove("hidden");
      return;
    }
    if (!channelName || !data.channels[channelName]) return;

    hint.classList.add("hidden");
    el("bands-compare-plot").classList.remove("hidden");

    const ch = data.channels[channelName];
    const bandNames = data.band_names;
    const closedVals = bandNames.map((b) => ch[b].closed_uv);
    const openVals = bandNames.map((b) => ch[b].open_uv);
    const labels = bandNames.map((b) => BAND_LABELS[b]);

    const traces = [
      {
        x: labels, y: closedVals, type: "bar", name: "Olhos fechados",
        marker: { color: "#5aa9ff" },
        hovertemplate: "%{y:.1f} µV<extra>Olhos fechados</extra>",
      },
      {
        x: labels, y: openVals, type: "bar", name: "Olhos abertos",
        marker: { color: "#facc15" },
        hovertemplate: "%{y:.1f} µV<extra>Olhos abertos</extra>",
      },
    ];
    const layout = {
      barmode: "group",
      margin: { t: 20, r: 20, b: 40, l: 60 },
      paper_bgcolor: "#171d2c",
      plot_bgcolor: "#171d2c",
      font: { color: "#e6e9f2", size: 11 },
      legend: { orientation: "h", y: 1.15 },
      yaxis: { title: { text: "Potência RMS (µV)" }, gridcolor: "#242c40", zeroline: false },
      xaxis: { gridcolor: "#242c40" },
    };
    Plotly.react(el("bands-compare-plot"), traces, layout, { responsive: true, displaylogo: false });
  }

  // ---- artifact marking (blinks, jaw clench, etc.) -----------------------
  // Anghinah et al. (Arq. Neuropsiquiatr., "Artefatos biológicos no EEG
  // quantitativo"): artifacts distort spectral/topographic estimates and
  // can't be corrected after the FFT/bandpass step runs, so the reliable
  // fix is visual identification *before* that step, not automatic
  // detection. This lets a student drag over a contaminated stretch on the
  // raw/filtered trace (Grade or Foco) and exclude it from every band-power
  // calculation everywhere -- Bandas, the closed/open comparison, and the
  // cross-test comparison.

  function setArtifactMarking(on) {
    state.markingArtifact = on;
    el("mark-artifact-btn").classList.toggle("active", on);
    el("artifact-mark-hint").classList.toggle("hidden", !on);
    // Re-render the currently visible single-recording plot so its dragmode
    // actually switches to/from "drawrect".
    if (state.lastData && (state.viewMode === "grid" || state.viewMode === "focus")) {
      renderAll(state.lastData, state.lastChannels);
    }
  }

  async function saveArtifactRanges() {
    if (!state.token) return;
    try {
      await postJSON("/api/artifacts/save/", { token: state.token, ranges: state.artifactRanges });
    } catch (err) {
      showError("process-error", err.message);
    }
    renderArtifactsBar();
    // Re-run the pipeline so band power / comparisons everywhere reflect the
    // updated exclusion immediately -- applyFilters() already re-fetches
    // Bandas/comparison data and re-renders the active view when needed.
    await applyFilters();
  }

  function addArtifactRange(t0, t1) {
    state.artifactRanges.push([Math.min(t0, t1), Math.max(t0, t1)]);
    saveArtifactRanges();
  }

  function removeArtifactRange(index) {
    state.artifactRanges.splice(index, 1);
    saveArtifactRanges();
  }

  function renderArtifactsBar() {
    const bar = el("artifacts-bar");
    const list = el("artifacts-list");
    list.innerHTML = "";
    if (!state.artifactRanges.length) {
      bar.classList.add("hidden");
      return;
    }
    bar.classList.remove("hidden");
    state.artifactRanges.forEach(([t0, t1], i) => {
      const chip = document.createElement("span");
      chip.className = "artifact-chip";
      const label = document.createElement("span");
      label.textContent = `${t0.toFixed(1)}–${t1.toFixed(1)}s`;
      const delBtn = document.createElement("button");
      delBtn.type = "button";
      delBtn.textContent = "✕";
      delBtn.title = "Remover esta marcação de artefato";
      delBtn.addEventListener("click", () => removeArtifactRange(i));
      chip.appendChild(label);
      chip.appendChild(delBtn);
      list.appendChild(chip);
    });
  }

  // Plotly's shape-drawing mode (dragmode:"drawrect") appends a plain shape
  // to the div's own layout.shapes when the user finishes a drag -- it has
  // no `_kind` tag (every shape *we* add programmatically does, see
  // artifactShapes/markerConditionLayout), so any untagged shape found after
  // a relayout is exactly one the user just drew.
  function wireArtifactDrawing(plotDiv) {
    // Plotly.react() preserves the div's identity (and any .on() listeners
    // already attached to it) across re-renders, so without this guard every
    // render would stack another listener and one drawn rectangle would fire
    // addArtifactRange() once per accumulated listener.
    if (plotDiv._artifactWired) return;
    plotDiv._artifactWired = true;
    plotDiv.on("plotly_relayout", () => {
      if (!state.markingArtifact) return;
      const shapes = plotDiv.layout.shapes || [];
      const drawn = shapes.find((s) => !s._kind);
      if (!drawn) return;
      addArtifactRange(drawn.x0, drawn.x1);
    });
  }

  function setupArtifactMarking() {
    el("mark-artifact-btn").addEventListener("click", () => setArtifactMarking(!state.markingArtifact));
  }

  function setViewMode(mode) {
    state.viewMode = mode;
    el("mode-grid-btn").classList.toggle("active", mode === "grid");
    el("mode-focus-btn").classList.toggle("active", mode === "focus");
    el("mode-bands-btn").classList.toggle("active", mode === "bands");
    el("mode-compare-btn").classList.toggle("active", mode === "compare");
    el("focus-nav").classList.toggle("hidden", mode === "grid");
    el("focus-sat-badge").classList.toggle("hidden", mode === "compare");
    el("plot").classList.toggle("hidden", mode !== "grid");
    el("focus-plot").classList.toggle("hidden", mode !== "focus");
    el("bands-plot").classList.toggle("hidden", mode !== "bands");
    el("bands-legend").classList.toggle("hidden", mode !== "bands");
    el("main-legend").classList.toggle("hidden", mode === "bands" || mode === "compare");
    el("bands-compare-wrap").classList.toggle("hidden", mode !== "bands");
    el("compare-plot-wrap").classList.toggle("hidden", mode !== "compare");
    el("compare-legend").classList.toggle("hidden", mode !== "compare");
    el("timeline-wrap").classList.toggle("hidden", mode === "compare" || !state.lastData);
    el("mark-artifact-btn").classList.toggle("hidden", !(state.lastData && (mode === "grid" || mode === "focus")));

    if (mode === "compare") {
      renderCompareAll();
      return;
    }
    if (!state.lastData) return;
    const needsFetch = mode === "bands" && (!state.lastBandsData || !state.lastBandsCompareData);
    if (needsFetch) {
      Promise.all([fetchBands(), fetchBandsCompare()]).then(() => renderAll(state.lastData, state.lastChannels));
    } else {
      renderAll(state.lastData, state.lastChannels);
    }
  }

  function setFocusChannel(name) {
    const list = activeChannelList();
    if (!list.includes(name)) return;
    if (state.viewMode === "compare") {
      state.compareFocusChannel = name;
      el("focus-channel-select").value = name;
      if (state.lastCompareData) renderCompareAll();
    } else {
      state.focusChannel = name;
      el("focus-channel-select").value = name;
      if (state.lastData) renderAll(state.lastData, state.lastChannels);
    }
  }

  function setupViewControls() {
    el("mode-grid-btn").addEventListener("click", () => setViewMode("grid"));
    el("mode-focus-btn").addEventListener("click", () => setViewMode("focus"));
    el("mode-bands-btn").addEventListener("click", () => setViewMode("bands"));
    el("mode-compare-btn").addEventListener("click", () => setViewMode("compare"));
    el("focus-channel-select").addEventListener("change", (e) => setFocusChannel(e.target.value));
    el("focus-prev").addEventListener("click", () => {
      const list = activeChannelList();
      const current = state.viewMode === "compare" ? state.compareFocusChannel : state.focusChannel;
      const idx = list.indexOf(current);
      setFocusChannel(list[(idx - 1 + list.length) % list.length]);
    });
    el("focus-next").addEventListener("click", () => {
      const list = activeChannelList();
      const current = state.viewMode === "compare" ? state.compareFocusChannel : state.focusChannel;
      const idx = list.indexOf(current);
      setFocusChannel(list[(idx + 1) % list.length]);
    });
  }

  let timelineReady = false;
  function renderTimeline(data, channels, fullRange) {
    const t = data.t;
    const refChannel = channels[0];
    const y = data.channels[refChannel].filtered;

    const traces = [{
      x: t, y, type: "scattergl", mode: "lines",
      line: { color: "#5aa9ff", width: 1 },
      hoverinfo: "skip", name: refChannel,
    }];
    const shapes = state.markers.map((m) => ({
      type: "line", xref: "x", yref: "paper", x0: m.sample_time, x1: m.sample_time,
      y0: 0, y1: 1, line: { color: "#4ade80", width: 1, dash: "dot" },
    }));

    const layout = {
      margin: { t: 6, r: 20, b: 30, l: 60 },
      paper_bgcolor: "#171d2c",
      plot_bgcolor: "#171d2c",
      font: { color: "#e6e9f2", size: 10 },
      showlegend: false,
      shapes,
      yaxis: { title: { text: `${refChannel} (visão geral)`, font: { size: 9 } }, showticklabels: false },
      xaxis: {
        range: fullRange,
        title: { text: "Linha do tempo — arraste para navegar (s)" },
        rangeslider: {
          visible: true,
          range: fullRange,
          thickness: 0.55,
          bgcolor: "#0f1420",
          bordercolor: "#2a3348",
          borderwidth: 1,
        },
      },
    };

    const timelineDiv = el("timeline");
    Plotly.react(timelineDiv, traces, layout, { responsive: true, displaylogo: false });
    el("timeline-wrap").classList.remove("hidden");

    if (!timelineReady) {
      timelineDiv.on("plotly_relayout", (ev) => {
        let range = null;
        if (ev["xaxis.range"]) range = ev["xaxis.range"];
        else if (ev["xaxis.range[0]"] !== undefined && ev["xaxis.range[1]"] !== undefined) {
          range = [ev["xaxis.range[0]"], ev["xaxis.range[1]"]];
        }
        if (range) {
          const targetId = state.viewMode === "grid" ? "plot" : state.viewMode === "bands" ? "bands-plot" : "focus-plot";
          const target = el(targetId);
          Plotly.relayout(target, { "xaxis.range": range });
        }
      });
      timelineReady = true;
    }
  }

  async function downloadFiltered() {
    if (!state.token) return;
    const channels = selectedChannels();
    showError("process-error", "");
    try {
      const resp = await fetch("/api/download/", {
        method: "POST",
        headers: { "Content-Type": "application/json", "X-CSRFToken": csrftoken() },
        body: JSON.stringify({ token: state.token, channels, params: currentParams() }),
      });
      if (redirectToLoginIfExpired(resp)) return;
      if (!resp.ok) {
        const data = await resp.json().catch(() => ({ error: `Erro HTTP ${resp.status}` }));
        throw new Error(data.error || `Erro HTTP ${resp.status}`);
      }
      const blob = await resp.blob();
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = "eeg_filtered.csv";
      document.body.appendChild(a);
      a.click();
      a.remove();
      URL.revokeObjectURL(url);
    } catch (err) {
      showError("process-error", err.message);
    }
  }

  async function downloadBands() {
    if (!state.token) return;
    const channels = selectedChannels();
    showError("process-error", "");
    try {
      const resp = await fetch("/api/bands/download/", {
        method: "POST",
        headers: { "Content-Type": "application/json", "X-CSRFToken": csrftoken() },
        body: JSON.stringify({ token: state.token, channels, params: currentParams() }),
      });
      if (redirectToLoginIfExpired(resp)) return;
      if (!resp.ok) {
        const data = await resp.json().catch(() => ({ error: `Erro HTTP ${resp.status}` }));
        throw new Error(data.error || `Erro HTTP ${resp.status}`);
      }
      const blob = await resp.blob();
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = "eeg_bandas.csv";
      document.body.appendChild(a);
      a.click();
      a.remove();
      URL.revokeObjectURL(url);
    } catch (err) {
      showError("process-error", err.message);
    }
  }

  function setupFormulaRendering() {
    if (typeof renderMathInElement !== "function") return;
    renderMathInElement(document.body, {
      delimiters: [
        { left: "$$", right: "$$", display: true },
        { left: "$", right: "$", display: false },
      ],
      throwOnError: false,
    });
  }

  setupUpload();
  setupControlListeners();
  setupViewControls();
  setupTestsControls();
  setupArtifactMarking();
  setupFormulaRendering();
  fetchTestsList();
})();
