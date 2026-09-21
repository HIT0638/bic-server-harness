(() => {
  "use strict";

  const params = new URLSearchParams(window.location.search);
  const urlToken = params.get("token") || "";
  if (urlToken) {
    window.sessionStorage.setItem("sshbridge-token", urlToken);
    window.history.replaceState({}, "", window.location.pathname);
  }
  const token = urlToken ||
    window.sessionStorage.getItem("sshbridge-token") || "";

  const elements = {
    tree: document.querySelector("#tree"),
    profile: document.querySelector("#profile-name"),
    connectionDot: document.querySelector("#connection-dot"),
    reconnect: document.querySelector("#reconnect"),
    currentPath: document.querySelector("#current-path"),
    dirty: document.querySelector("#dirty-indicator"),
    editor: document.querySelector("#editor"),
    fileView: document.querySelector("#file-view"),
    readonlyView: document.querySelector("#readonly-view"),
    emptyState: document.querySelector("#empty-state"),
    save: document.querySelector("#save-file"),
    refresh: document.querySelector("#refresh-tree"),
    newFile: document.querySelector("#new-file"),
    newFolder: document.querySelector("#new-folder"),
    rename: document.querySelector("#rename-entry"),
    fileMeta: document.querySelector("#file-meta"),
    operationStatus: document.querySelector("#operation-status"),
    toast: document.querySelector("#toast"),
  };

  const state = {
    info: null,
    directories: new Map(),
    selectedPath: "/",
    selectedType: "dir",
    currentFile: null,
    dirty: false,
    toastTimer: null,
  };

  async function api(route, options = {}) {
    const headers = new Headers(options.headers || {});
    headers.set("X-SSHBridge-Token", token);
    if (options.body !== undefined) {
      headers.set("Content-Type", "application/json");
      options.body = JSON.stringify(options.body);
    }
    const response = await fetch(route, {...options, headers});
    let payload;
    try {
      payload = await response.json();
    } catch (_error) {
      throw {code: "BAD_RESPONSE", message: "服务返回了无效响应"};
    }
    if (!response.ok || !payload.ok) {
      const error = payload.error || {
        code: "REQUEST_FAILED",
        message: `请求失败 (${response.status})`,
      };
      if (error.code === "CONNECTION_PAUSED") {
        setConnectionPaused();
      }
      throw error;
    }
    return payload;
  }

  function queryPath(route, path) {
    return `${route}?path=${encodeURIComponent(path)}`;
  }

  function joinPath(parent, name) {
    return parent === "/" ? `/${name}` : `${parent}/${name}`;
  }

  function parentPath(path) {
    if (path === "/") return "/";
    const index = path.lastIndexOf("/");
    return index <= 0 ? "/" : path.slice(0, index);
  }

  function baseName(path) {
    if (path === "/") return "/";
    return path.slice(path.lastIndexOf("/") + 1);
  }

  function validName(name) {
    return Boolean(
      name && name !== "." && name !== ".." &&
      !name.includes("/") && !name.includes("\\") && !name.includes("\0")
    );
  }

  function selectedDirectory() {
    return state.selectedType === "dir"
      ? state.selectedPath
      : parentPath(state.selectedPath);
  }

  function setBusy(message) {
    elements.operationStatus.textContent = message || "";
  }

  function showToast(message, error = false) {
    window.clearTimeout(state.toastTimer);
    elements.toast.textContent = message;
    elements.toast.classList.toggle("error", error);
    elements.toast.classList.add("visible");
    state.toastTimer = window.setTimeout(() => {
      elements.toast.classList.remove("visible");
    }, 3200);
  }

  function errorMessage(error) {
    const code = error && error.code ? `[${error.code}] ` : "";
    return `${code}${error && error.message ? error.message : "操作失败"}`;
  }

  function setConnected(connected) {
    elements.connectionDot.classList.toggle("connected", connected);
    elements.connectionDot.classList.toggle("failed", !connected);
    if (connected) {
      elements.reconnect.hidden = true;
      if (state.info) elements.profile.textContent = state.info.profile;
    }
  }

  function setConnectionPaused() {
    setConnected(false);
    elements.profile.textContent = "连接已暂停";
    elements.reconnect.hidden = false;
  }

  async function reconnect() {
    setBusy("重新连接");
    elements.reconnect.disabled = true;
    try {
      await api("/api/reconnect", {method: "POST", body: {}});
      setConnected(true);
      state.directories.clear();
      await loadDirectory("/", true);
      showToast("连接已恢复");
    } catch (error) {
      if (error.code === "CONNECTION_PAUSED" ||
          error.code === "CONNECTION_RATE_LIMITED") {
        setConnectionPaused();
      }
      showToast(errorMessage(error), true);
    } finally {
      elements.reconnect.disabled = false;
      setBusy("");
    }
  }

  async function loadDirectory(path, force = false) {
    const existing = state.directories.get(path);
    if (existing && existing.loaded && !force) {
      existing.expanded = !existing.expanded;
      renderTree();
      return;
    }

    state.directories.set(path, {
      entries: existing ? existing.entries : [],
      loaded: false,
      expanded: true,
      loading: true,
      error: "",
    });
    renderTree();
    setBusy("读取目录");
    try {
      const result = await api(queryPath("/api/list", path));
      state.directories.set(path, {
        entries: result.entries,
        loaded: true,
        expanded: true,
        loading: false,
        error: "",
      });
      setConnected(true);
    } catch (error) {
      state.directories.set(path, {
        entries: [],
        loaded: false,
        expanded: true,
        loading: false,
        error: errorMessage(error),
      });
      setConnected(false);
      showToast(errorMessage(error), true);
    } finally {
      setBusy("");
      renderTree();
    }
  }

  function collapseDirectory(path) {
    const node = state.directories.get(path);
    if (!node) return;
    node.expanded = false;
    renderTree();
  }

  function renderTree() {
    elements.tree.replaceChildren();
    renderDirectoryNode("/", "workspace", 0);
  }

  function renderDirectoryNode(path, label, depth) {
    const node = state.directories.get(path);
    const expanded = Boolean(node && node.expanded);
    const row = createRow({
      path,
      name: label,
      type: "dir",
      depth,
      expanded,
      loading: Boolean(node && node.loading),
    });
    elements.tree.append(row);

    if (!node || !expanded) return;
    if (node.loading) {
      elements.tree.append(createTreeMessage("读取中", depth + 1));
      return;
    }
    if (node.error) {
      elements.tree.append(createTreeMessage(node.error, depth + 1, true));
      return;
    }
    for (const entry of node.entries) {
      const childPath = joinPath(path, entry.name);
      if (entry.type === "dir") {
        renderDirectoryNode(childPath, entry.name, depth + 1);
      } else {
        elements.tree.append(createRow({
          path: childPath,
          name: entry.name,
          type: entry.type,
          depth: depth + 1,
          expanded: false,
          loading: false,
        }));
      }
    }
  }

  function createTreeMessage(message, depth, error = false) {
    const item = document.createElement("div");
    item.className = error ? "tree-error" : "tree-loading";
    item.style.paddingLeft = `${14 + depth * 16}px`;
    item.textContent = message;
    return item;
  }

  function createRow(entry) {
    const row = document.createElement("div");
    row.className = "tree-row";
    row.setAttribute("role", "treeitem");
    row.setAttribute("aria-selected", String(state.selectedPath === entry.path));
    row.style.paddingLeft = `${6 + entry.depth * 16}px`;
    if (state.selectedPath === entry.path) row.classList.add("selected");

    const toggle = document.createElement("button");
    toggle.type = "button";
    toggle.className = "tree-toggle";
    if (entry.type === "dir") {
      toggle.textContent = entry.loading ? "…" : entry.expanded ? "▾" : "▸";
      toggle.title = entry.expanded ? "折叠" : "展开";
      toggle.setAttribute("aria-label", toggle.title);
      toggle.addEventListener("click", (event) => {
        event.stopPropagation();
        selectEntry(entry.path, "dir");
        if (entry.expanded) collapseDirectory(entry.path);
        else loadDirectory(entry.path);
      });
    } else {
      toggle.classList.add("empty");
      toggle.setAttribute("aria-hidden", "true");
    }

    const icon = document.createElement("span");
    icon.className = `entry-icon ${entry.type === "dir" ? "dir" : "file"}`;
    icon.setAttribute("aria-hidden", "true");

    const name = document.createElement("span");
    name.className = "tree-label";
    name.textContent = entry.name;
    name.title = entry.path;

    row.append(toggle, icon, name);
    row.addEventListener("click", () => {
      if (entry.type === "dir") {
        selectEntry(entry.path, "dir");
        if (entry.expanded) collapseDirectory(entry.path);
        else loadDirectory(entry.path);
      } else {
        openFile(entry.path);
      }
    });
    return row;
  }

  function selectEntry(path, type) {
    state.selectedPath = path;
    state.selectedType = type;
    renderTree();
  }

  async function openFile(path, force = false) {
    if (!force && state.currentFile && state.currentFile.path === path) {
      selectEntry(path, "file");
      return;
    }
    if (!confirmDiscard()) return;
    selectEntry(path, "file");
    setBusy("读取文件");
    try {
      const stat = await api(queryPath("/api/stat", path));
      if (stat.size > state.info.max_read_bytes) {
        showReadonly(path, `文件过大 (${formatBytes(stat.size)})`);
        return;
      }
      const result = await api(queryPath("/api/read", path));
      if (result.binary) {
        showReadonly(path, `二进制文件 (${formatBytes(result.size)})`);
        return;
      }
      state.currentFile = {
        path,
        mtime: result.mtime,
        size: result.size,
        content: result.content,
      };
      state.dirty = false;
      elements.editor.value = result.content;
      elements.editor.hidden = false;
      elements.readonlyView.hidden = true;
      elements.fileView.hidden = false;
      elements.emptyState.hidden = true;
      elements.currentPath.textContent = path;
      elements.fileMeta.textContent =
        `${formatBytes(result.size)} · UTF-8 · ${formatTime(result.mtime)}`;
      updateDirtyState();
      elements.editor.focus();
      setConnected(true);
    } catch (error) {
      showToast(errorMessage(error), true);
    } finally {
      setBusy("");
    }
  }

  function showReadonly(path, message) {
    state.currentFile = null;
    state.dirty = false;
    elements.editor.hidden = true;
    elements.readonlyView.hidden = false;
    elements.readonlyView.textContent = message;
    elements.fileView.hidden = false;
    elements.emptyState.hidden = true;
    elements.currentPath.textContent = path;
    elements.fileMeta.textContent = message;
    updateDirtyState();
  }

  function confirmDiscard() {
    return !state.dirty || window.confirm("当前文件尚未保存，放弃修改？");
  }

  function updateDirtyState() {
    document.documentElement.dataset.dirty = state.dirty ? "true" : "false";
    elements.dirty.classList.toggle("active", state.dirty);
    elements.save.disabled = !state.currentFile || !state.dirty;
  }

  async function saveFile(force = false) {
    if (!state.currentFile || !state.dirty) return;
    setBusy("保存中");
    try {
      const result = await api("/api/write", {
        method: "POST",
        body: {
          path: state.currentFile.path,
          content: elements.editor.value,
          expected_mtime: state.currentFile.mtime,
          expected_size: state.currentFile.size,
          force,
        },
      });
      state.currentFile.content = elements.editor.value;
      state.currentFile.mtime = result.mtime;
      state.currentFile.size = result.size;
      state.dirty = false;
      updateDirtyState();
      elements.fileMeta.textContent =
        `${formatBytes(result.size)} · UTF-8 · ${formatTime(result.mtime)}`;
      await refreshDirectory(parentPath(state.currentFile.path));
      showToast("已保存");
    } catch (error) {
      if (error.code === "CONFLICT" && !force) {
        const overwrite = window.confirm(
          "远端文件已变化。是否强制覆盖远端版本？"
        );
        if (overwrite) return saveFile(true);
      } else {
        showToast(errorMessage(error), true);
      }
    } finally {
      setBusy("");
    }
  }

  async function refreshDirectory(path) {
    await loadDirectory(path, true);
  }

  async function refreshSelection() {
    const directory = selectedDirectory();
    await refreshDirectory(directory);
    if (state.selectedType === "file" && !state.dirty) {
      await openFile(state.selectedPath, true);
    }
  }

  async function createFile() {
    const directory = selectedDirectory();
    const name = window.prompt("文件名");
    if (name === null) return;
    if (!validName(name)) {
      showToast("文件名无效", true);
      return;
    }
    const path = joinPath(directory, name);
    setBusy("新建文件");
    try {
      try {
        await api(queryPath("/api/stat", path));
        showToast("目标已存在", true);
        return;
      } catch (error) {
        if (error.code !== "NOT_FOUND") throw error;
      }
      await api("/api/write", {
        method: "POST",
        body: {path, content: ""},
      });
      await refreshDirectory(directory);
      await openFile(path);
      showToast("文件已创建");
    } catch (error) {
      showToast(errorMessage(error), true);
    } finally {
      setBusy("");
    }
  }

  async function createFolder() {
    const directory = selectedDirectory();
    const name = window.prompt("目录名");
    if (name === null) return;
    if (!validName(name)) {
      showToast("目录名无效", true);
      return;
    }
    setBusy("新建目录");
    try {
      await api("/api/mkdir", {
        method: "POST",
        body: {path: joinPath(directory, name)},
      });
      await refreshDirectory(directory);
      showToast("目录已创建");
    } catch (error) {
      showToast(errorMessage(error), true);
    } finally {
      setBusy("");
    }
  }

  async function renameSelected() {
    if (state.selectedPath === "/") {
      showToast("不能重命名工作区根目录", true);
      return;
    }
    if (state.dirty && !confirmDiscard()) return;
    const oldPath = state.selectedPath;
    const name = window.prompt("新名称", baseName(oldPath));
    if (name === null || name === baseName(oldPath)) return;
    if (!validName(name)) {
      showToast("名称无效", true);
      return;
    }
    const destination = joinPath(parentPath(oldPath), name);
    setBusy("重命名");
    try {
      await api("/api/move", {
        method: "POST",
        body: {src: oldPath, dst: destination},
      });
      if (state.currentFile && state.currentFile.path === oldPath) {
        state.currentFile.path = destination;
        elements.currentPath.textContent = destination;
      }
      state.selectedPath = destination;
      const oldDirectory = state.directories.get(oldPath);
      if (oldDirectory) {
        state.directories.delete(oldPath);
        state.directories.set(destination, oldDirectory);
      }
      await refreshDirectory(parentPath(oldPath));
      showToast("已重命名");
    } catch (error) {
      showToast(errorMessage(error), true);
    } finally {
      setBusy("");
    }
  }

  function formatBytes(value) {
    if (!Number.isFinite(value)) return "-";
    if (value < 1024) return `${value} B`;
    if (value < 1024 * 1024) return `${(value / 1024).toFixed(1)} KiB`;
    return `${(value / (1024 * 1024)).toFixed(1)} MiB`;
  }

  function formatTime(seconds) {
    if (!seconds) return "未知时间";
    return new Date(seconds * 1000).toLocaleString();
  }

  async function initialize() {
    if (!token) {
      setConnected(false);
      elements.profile.textContent = "缺少访问 token";
      showToast("请使用服务启动时输出的完整 URL", true);
      return;
    }
    try {
      state.info = await api("/api/info");
      elements.profile.textContent = state.info.profile;
      setConnected(true);
      await loadDirectory("/");
    } catch (error) {
      setConnected(false);
      elements.profile.textContent = "连接失败";
      showToast(errorMessage(error), true);
    }
  }

  elements.editor.addEventListener("input", () => {
    if (!state.currentFile) return;
    state.dirty = elements.editor.value !== state.currentFile.content;
    updateDirtyState();
  });
  elements.save.addEventListener("click", () => saveFile());
  elements.refresh.addEventListener("click", refreshSelection);
  elements.newFile.addEventListener("click", createFile);
  elements.newFolder.addEventListener("click", createFolder);
  elements.rename.addEventListener("click", renameSelected);
  elements.reconnect.addEventListener("click", reconnect);
  window.addEventListener("keydown", (event) => {
    if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === "s") {
      event.preventDefault();
      saveFile();
    }
  });
  window.addEventListener("beforeunload", (event) => {
    if (!state.dirty) return;
    event.preventDefault();
  });

  updateDirtyState();
  initialize();
})();
