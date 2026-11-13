class SnapmakerUploadPanel extends HTMLElement {
  set hass(hass) {
    this._hass = hass;
    if (!this._built) this._build();
  }

  set narrow(_) {}
  set panel(_) {}

  async _build() {
    this._built = true;
    this.attachShadow({ mode: "open" }).innerHTML = `
      <style>
        :host { display: block; padding: 16px; color: var(--primary-text-color);
                background: var(--primary-background-color); min-height: 100vh; box-sizing: border-box; }
        h1 { font-weight: 400; margin: 0 0 16px; }
        select, button { font: inherit; padding: 8px; margin-bottom: 16px; }
        #drop { border: 2px dashed var(--divider-color); border-radius: 12px; padding: 64px 16px;
                text-align: center; cursor: pointer; max-width: 600px; }
        #drop.over { border-color: var(--primary-color); background: var(--secondary-background-color); }
        #status { margin-top: 16px; white-space: pre-wrap; }
        .error { color: var(--error-color); }
      </style>
      <h1>Snapmaker Artisan: upload G-code</h1>
      <div><select id="printer"></select></div>
      <div id="drop">Drop a G-code file here or click to choose<br><small>.gcode, .nc, .cnc</small></div>
      <input id="file" type="file" accept=".gcode,.nc,.cnc,.txt" hidden>
      <div id="status"></div>`;
    const $ = (id) => this.shadowRoot.getElementById(id);
    this._select = $("printer");
    this._status = $("status");
    const drop = $("drop");
    const input = $("file");
    drop.addEventListener("click", () => input.click());
    input.addEventListener("change", () => input.files[0] && this._upload(input.files[0]));
    drop.addEventListener("dragover", (e) => { e.preventDefault(); drop.classList.add("over"); });
    drop.addEventListener("dragleave", () => drop.classList.remove("over"));
    drop.addEventListener("drop", (e) => {
      e.preventDefault();
      drop.classList.remove("over");
      if (e.dataTransfer.files[0]) this._upload(e.dataTransfer.files[0]);
    });
    try {
      const printers = await this._hass.callApi("GET", "snapmaker_artisan/printers");
      this._select.innerHTML = printers
        .map((p) => `<option value="${p.entry_id}">${p.title}${p.connected ? "" : " (offline)"}</option>`)
        .join("");
      if (!printers.length) this._show("No printer configured.", true);
    } catch (err) {
      this._show(`Could not load printers: ${err.message || err}`, true);
    }
  }

  _show(text, error = false) {
    this._status.textContent = text;
    this._status.className = error ? "error" : "";
  }

  async _upload(file) {
    if (!this._select.value) return this._show("Select a printer first.", true);
    const form = new FormData();
    form.append("entry_id", this._select.value);
    form.append("file", file, file.name);
    this._show(`Uploading ${file.name} (${(file.size / 1048576).toFixed(1)} MB)…`);
    try {
      const response = await this._hass.fetchWithAuth("/api/snapmaker_artisan/upload", {
        method: "POST",
        body: form,
      });
      const body = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(body.error || response.statusText);
      this._show(`Uploaded ${file.name}. Start the print on the touchscreen.`);
    } catch (err) {
      this._show(`Upload failed: ${err.message || err}`, true);
    }
  }
}

customElements.define("snapmaker-upload-panel", SnapmakerUploadPanel);
