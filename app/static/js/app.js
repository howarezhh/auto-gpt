(function () {
    let page = document.body.dataset.page;
    let pageCleanupHandlers = [];
    const authContext = {
        isAuthenticated: document.body.dataset.authenticated === "true",
        userRole: document.body.dataset.userRole || "",
    };
    const PUBLIC_ROUTE_PREFIXES = ["/login", "/register", "/setup-admin"];
    const ADMIN_ROUTE_PREFIXES = ["/providers", "/models", "/settings", "/playground", "/docs", "/api-keys", "/logs", "/alerts", "/conversations", "/users", "/audit-logs"];

    const ROUTE_MODE_LABELS = {
        manual: "手动优先",
        failover: "故障切换",
        weighted: "权重分流",
        sticky: "会话粘滞",
    };

    const LOG_TYPE_LABELS = {
        health_check: "健康检查",
        chat: "对话请求",
        responses: "响应请求",
        embeddings: "历史向量请求（已下线）",
        health_check_provider: "渠道健康检查",
        health_check_model: "模型健康检查",
        proxy_generic: "通用代理日志",
        api_client_auth: "密钥鉴权失败日志",
    };

    const HEALTH_STATUS_LABELS = {
        healthy: "健康",
        degraded: "降级",
        unhealthy: "异常",
        unknown: "未知",
    };

    const CIRCUIT_STATE_LABELS = {
        closed: "闭合",
        open: "已熔断",
        half_open: "半开探测",
        unknown: "未知",
    };

    const API_CLIENT_AUTH_RESULT_LABELS = {
        authenticated: "鉴权通过",
        invalid_api_key: "无效密钥",
        key_disabled: "密钥已禁用",
        key_expired: "密钥已过期",
        insufficient_quota: "额度不足",
        insufficient_balance: "余额不足",
        no_authorized_provider: "未授权渠道",
    };

    const API_KEY_STATUS_LABELS = {
        active: "正常可用",
        disabled: "已禁用",
        expired: "已过期",
        quota_exhausted: "额度耗尽",
        cost_quota_exhausted: "金额额度耗尽",
        balance_exhausted: "余额耗尽",
        unbound: "未绑定渠道",
    };
    const API_KEY_RAW_PREFIX = "sk-aotu-";
    const API_KEY_RAW_MIN_LENGTH = 24;
    const API_KEY_RAW_MAX_LENGTH = 128;
    const API_KEY_RAW_PATTERN = /^[A-Za-z0-9\-_]+$/;

    const api = {
        get: async (url) => parseResponse(await fetch(url, { cache: "no-store", headers: { "Cache-Control": "no-cache" } })),
        post: async (url, data) => parseResponse(await fetch(url, withJson("POST", data))),
        put: async (url, data) => parseResponse(await fetch(url, withJson("PUT", data))),
        delete: async (url) => parseResponse(await fetch(url, { method: "DELETE" })),
    };

    function debounce(callback, wait = 300) {
        let timerId = null;
        return (...args) => {
            if (timerId) {
                window.clearTimeout(timerId);
            }
            timerId = window.setTimeout(() => callback(...args), wait);
        };
    }

    function getResolvedTheme() {
        const storedTheme = window.localStorage.getItem("aotu-theme");
        if (storedTheme === "light" || storedTheme === "dark") {
            return storedTheme;
        }
        return window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
    }

    function applyTheme(theme) {
        document.documentElement.dataset.theme = theme;
        document.documentElement.style.colorScheme = theme;
        const label = document.getElementById("theme-toggle-label");
        const button = document.getElementById("theme-toggle");
        if (label) {
            label.textContent = theme === "dark" ? "浅色模式" : "暗黑模式";
        }
        if (button) {
            button.setAttribute("aria-label", theme === "dark" ? "切换到浅色模式" : "切换到暗黑模式");
            button.dataset.theme = theme;
        }
    }

    function initThemeToggle() {
        applyTheme(getResolvedTheme());
        const toggle = document.getElementById("theme-toggle");
        if (!toggle || toggle.dataset.boundThemeToggle === "true") return;
        toggle.dataset.boundThemeToggle = "true";
        toggle.addEventListener("click", () => {
            const nextTheme = document.documentElement.dataset.theme === "dark" ? "light" : "dark";
            window.localStorage.setItem("aotu-theme", nextTheme);
            applyTheme(nextTheme);
        });
        const mediaQuery = window.matchMedia("(prefers-color-scheme: dark)");
        const syncSystemTheme = (event) => {
            const storedTheme = window.localStorage.getItem("aotu-theme");
            if (storedTheme === "light" || storedTheme === "dark") return;
            applyTheme(event.matches ? "dark" : "light");
        };
        if (typeof mediaQuery.addEventListener === "function") {
            mediaQuery.addEventListener("change", syncSystemTheme);
        } else if (typeof mediaQuery.addListener === "function") {
            mediaQuery.addListener(syncSystemTheme);
        }
    }

    function initSiteNavigation() {
        const toggle = document.getElementById("site-nav-toggle");
        const panel = document.getElementById("site-nav-panel");
        if (!toggle || !panel || toggle.dataset.boundSiteNav === "true") return;

        const closeNav = () => {
            panel.classList.remove("is-open");
            toggle.classList.remove("is-open");
            toggle.setAttribute("aria-expanded", "false");
        };

        const openNav = () => {
            panel.classList.add("is-open");
            toggle.classList.add("is-open");
            toggle.setAttribute("aria-expanded", "true");
        };

        toggle.dataset.boundSiteNav = "true";
        toggle.addEventListener("click", () => {
            if (panel.classList.contains("is-open")) {
                closeNav();
                return;
            }
            openNav();
        });

        document.addEventListener("click", (event) => {
            if (!panel.classList.contains("is-open")) return;
            if (panel.contains(event.target) || toggle.contains(event.target)) return;
            closeNav();
        });

        window.addEventListener("resize", () => {
            if (window.innerWidth > 992) closeNav();
        });
    }

    function createModalManager() {
        let activeController = null;

        function getFocusableElements(container) {
            if (!container) return [];
            return Array.from(
                container.querySelectorAll(
                    'a[href], button:not([disabled]), textarea:not([disabled]), input:not([disabled]), select:not([disabled]), [tabindex]:not([tabindex="-1"])'
                )
            ).filter((element) => !element.hasAttribute("hidden") && element.getAttribute("aria-hidden") !== "true");
        }

        function lockScroll() {
            document.body.classList.add("modal-open");
        }

        function unlockScroll() {
            if (!activeController) {
                document.body.classList.remove("modal-open");
            }
        }

        document.addEventListener("keydown", (event) => {
            if (!activeController) return;

            if (event.key === "Escape") {
                event.preventDefault();
                activeController.close({ reason: "escape" });
                return;
            }

            if (event.key !== "Tab") return;
            const focusable = getFocusableElements(activeController.dialog);
            if (!focusable.length) return;
            const currentIndex = focusable.indexOf(document.activeElement);
            if (event.shiftKey) {
                if (currentIndex <= 0) {
                    event.preventDefault();
                    focusable[focusable.length - 1].focus();
                }
                return;
            }
            if (currentIndex === focusable.length - 1) {
                event.preventDefault();
                focusable[0].focus();
            }
        });

        function register({
            modal,
            dialog,
            closeOnBackdrop = true,
            getInitialFocus,
            beforeClose,
            afterOpen,
            afterClose,
        }) {
            if (!modal || !dialog) {
                return {
                    open() {},
                    close() { return false; },
                    isOpen() { return false; },
                };
            }

            const controller = {
                modal,
                dialog,
                trigger: null,
                closeOnBackdrop,
                open(trigger = document.activeElement) {
                    controller.trigger = trigger instanceof HTMLElement ? trigger : null;
                    modal.classList.remove("hidden");
                    modal.setAttribute("aria-hidden", "false");
                    activeController = controller;
                    lockScroll();
                    window.requestAnimationFrame(() => {
                        const nextFocus = typeof getInitialFocus === "function"
                            ? getInitialFocus()
                            : getFocusableElements(dialog)[0] || dialog;
                        nextFocus?.focus?.();
                    });
                    afterOpen?.();
                },
                close({ force = false, reason = "programmatic" } = {}) {
                    if (!force && beforeClose?.({ reason }) === false) {
                        return false;
                    }
                    modal.classList.add("hidden");
                    modal.setAttribute("aria-hidden", "true");
                    if (activeController === controller) {
                        activeController = null;
                    }
                    unlockScroll();
                    afterClose?.({ reason });
                    controller.trigger?.focus?.();
                    return true;
                },
                isOpen() {
                    return !modal.classList.contains("hidden");
                },
            };

            modal.addEventListener("click", (event) => {
                if (event.target !== modal || !controller.closeOnBackdrop) return;
                controller.close({ reason: "backdrop" });
            });

            return controller;
        }

        return { register };
    }

    const modalManager = createModalManager();
    let healthCheckResultModalController = null;
    let healthCheckResultModalNode = null;

    function wait(ms) {
        return new Promise((resolve) => window.setTimeout(resolve, ms));
    }

    function ensureHealthCheckResultModalController() {
        const modal = document.getElementById("provider-test-result-modal");
        const titleNode = document.getElementById("provider-test-result-modal-title");
        const contentNode = document.getElementById("provider-test-result-modal-content");
        if (!modal || !titleNode || !contentNode) {
            return null;
        }
        if (healthCheckResultModalController && healthCheckResultModalNode === modal) {
            return { controller: healthCheckResultModalController, titleNode, contentNode };
        }
        healthCheckResultModalNode = modal;
        healthCheckResultModalController = modalManager.register({
            modal,
            dialog: modal.querySelector('[role="dialog"]'),
            getInitialFocus: () => document.getElementById("provider-test-result-modal-close"),
            afterClose: () => {
                contentNode.innerHTML = "";
            },
        });
        const closeBtn = document.getElementById("provider-test-result-modal-close");
        if (closeBtn && closeBtn.dataset.boundHealthCheckResultClose !== "true") {
            closeBtn.dataset.boundHealthCheckResultClose = "true";
            closeBtn.addEventListener("click", () => {
                healthCheckResultModalController?.close();
            });
        }
        return { controller: healthCheckResultModalController, titleNode, contentNode };
    }

    function openHealthCheckResultModal(title, html, trigger = document.activeElement) {
        const modalState = ensureHealthCheckResultModalController();
        if (!modalState) return;
        const { controller, titleNode, contentNode } = modalState;
        titleNode.textContent = title;
        contentNode.innerHTML = html;
        if (controller.isOpen()) {
            enhanceInteractiveButtons(contentNode);
            return;
        }
        controller.open(trigger);
        enhanceInteractiveButtons(contentNode);
    }

    function closeHealthCheckResultModal(options = {}) {
        healthCheckResultModalController?.close(options);
    }

    function withJson(method, data, extraHeaders = {}) {
        return {
            method,
            cache: "no-store",
            headers: { "Content-Type": "application/json", "Cache-Control": "no-cache", ...extraHeaders },
            body: JSON.stringify(data ?? {}),
        };
    }

    function matchRoute(pathname, prefix) {
        return pathname === prefix || pathname.startsWith(`${prefix}/`);
    }

    function getRouteRole(pathname) {
        if (PUBLIC_ROUTE_PREFIXES.some((prefix) => matchRoute(pathname, prefix))) {
            return "public";
        }
        if (matchRoute(pathname, "/user")) {
            return "user";
        }
        if (pathname === "/" || ADMIN_ROUTE_PREFIXES.some((prefix) => matchRoute(pathname, prefix))) {
            return "admin";
        }
        return "public";
    }

    function getRoleHomePath(role = authContext.userRole) {
        return role === "user" ? "/user" : "/";
    }

    function buildLoginPath(targetPath = `${window.location.pathname}${window.location.search}`) {
        return `/login?next=${encodeURIComponent(targetPath)}`;
    }

    function resolveRouteRedirect(targetUrl) {
        const target = new URL(targetUrl, window.location.origin);
        const routeRole = getRouteRole(target.pathname);
        if (routeRole === "public") {
            return null;
        }
        if (!authContext.isAuthenticated) {
            return buildLoginPath(`${target.pathname}${target.search}`);
        }
        if (routeRole !== authContext.userRole) {
            return getRoleHomePath();
        }
        return null;
    }

    async function parseResponse(response) {
        const text = await response.text();
        const data = text ? safeJsonParse(text) ?? text : null;
        if (response.status === 401) {
            window.location.href = buildLoginPath();
            throw new Error("登录状态已失效，请重新登录");
        }
        if (response.status === 403 && authContext.isAuthenticated) {
            window.location.href = getRoleHomePath();
            throw new Error("当前账号无权访问该内容");
        }
        if (!response.ok) {
            const detail = typeof data === "object" && data ? data.detail ?? data : data;
            throw new Error(typeof detail === "string" ? detail : JSON.stringify(detail, null, 2));
        }
        return data;
    }

    async function parseErrorResponse(response) {
        const text = await response.text();
        const data = text ? safeJsonParse(text) ?? text : null;
        const detail = typeof data === "object" && data ? data.detail ?? data : data;
        return typeof detail === "string" ? detail : JSON.stringify(detail, null, 2);
    }

    async function streamJsonLines(url, data, onEvent) {
        const response = await fetch(url, withJson("POST", data));
        if (response.status === 401) {
            window.location.href = buildLoginPath();
            throw new Error("登录状态已失效，请重新登录");
        }
        if (response.status === 403 && authContext.isAuthenticated) {
            window.location.href = getRoleHomePath();
            throw new Error("当前账号无权访问该内容");
        }
        if (!response.ok) {
            throw new Error(await parseErrorResponse(response));
        }
        const reader = response.body?.getReader();
        if (!reader) {
            throw new Error("健康检查流式响应不可用");
        }
        const decoder = new TextDecoder();
        let buffer = "";
        while (true) {
            const { value, done } = await reader.read();
            if (done) break;
            buffer += decoder.decode(value, { stream: true });
            let lineBreakIndex = buffer.indexOf("\n");
            while (lineBreakIndex >= 0) {
                const line = buffer.slice(0, lineBreakIndex).trim();
                buffer = buffer.slice(lineBreakIndex + 1);
                if (line) {
                    const payload = safeJsonParse(line);
                    if (payload) {
                        onEvent(payload);
                    }
                }
                lineBreakIndex = buffer.indexOf("\n");
            }
        }
        const tail = buffer.trim();
        if (tail) {
            const payload = safeJsonParse(tail);
            if (payload) {
                onEvent(payload);
            }
        }
    }

    function safeJsonParse(text) {
        try {
            return JSON.parse(text);
        } catch {
            return null;
        }
    }

    function syncResponsiveTableLabels(scope = document) {
        const tables = scope.querySelectorAll(".table-shell table, .table-responsive table");
        tables.forEach((table) => {
            const headers = Array.from(table.querySelectorAll("thead th")).map((cell) => cell.textContent.replace(/\s+/g, " ").trim());
            if (!headers.length) return;
            table.querySelectorAll("tbody tr").forEach((row) => {
                const cells = Array.from(row.querySelectorAll("td"));
                cells.forEach((cell, index) => {
                    cell.dataset.label = headers[index] || "";
                });
            });
        });
    }

    let responsiveTableSyncHandle = 0;
    function scheduleResponsiveTableSync(scope = document) {
        if (responsiveTableSyncHandle) {
            window.cancelAnimationFrame(responsiveTableSyncHandle);
        }
        responsiveTableSyncHandle = window.requestAnimationFrame(() => {
            responsiveTableSyncHandle = 0;
            syncResponsiveTableLabels(scope);
        });
    }

    function showToast(message, type = "success") {
        const stack = document.getElementById("toast-stack");
        if (!stack) return;
        while (stack.children.length >= 3) {
            stack.firstElementChild?.remove();
        }
        const node = document.createElement("div");
        node.className = `toast toast-${type}`;
        node.setAttribute("role", "status");
        node.textContent = message;
        stack.appendChild(node);
        setTimeout(() => node.remove(), 2800);
    }

    function setButtonTransientFeedback(button, status = "success", options = {}) {
        if (!button) return;
        const {
            successText = "已完成",
            errorText = "失败",
            duration = 1400,
        } = options;
        const nextText = status === "error" ? errorText : successText;
        window.clearTimeout(Number(button.dataset.feedbackTimer || 0));
        if (!button.dataset.feedbackOriginalText) {
            button.dataset.feedbackOriginalText = button.textContent;
        }
        button.classList.remove("is-action-success", "is-action-error");
        button.classList.add(status === "error" ? "is-action-error" : "is-action-success");
        button.textContent = nextText;
        button.dataset.feedbackTimer = String(window.setTimeout(() => {
            button.classList.remove("is-action-success", "is-action-error");
            button.textContent = button.dataset.feedbackOriginalText || button.textContent;
            delete button.dataset.feedbackTimer;
        }, duration));
    }

    function formatTestResultLabel(result, fallbackName) {
        const statusCode = result?.status_code ?? "-";
        const latencyMs = result?.latency_ms ?? "-";
        const statusText = result?.success ? "成功" : "失败";
        const healthText = result?.health_status ? `，健康 ${formatHealthStatusLabel(result.health_status)}` : "";
        const providerText = typeof result?.provider_success === "boolean"
            ? `，连通${result.provider_success ? "成功" : "失败"}`
            : "";
        const modelText = Number.isFinite(result?.models_total)
            ? `，模型 ${result.models_success ?? 0}/${result.models_total} 正常`
            : "";
        const message = result?.message ? `，${result.message}` : "";
        return `${fallbackName} 测试${statusText}${providerText}${healthText}，状态码 ${statusCode}，耗时 ${latencyMs} ms${modelText}${message}`;
    }

    function renderEndpointProbeHtml(endpointResults) {
        const items = Array.isArray(endpointResults) ? endpointResults : [];
        if (!items.length) {
            return '<div class="table-muted">未返回正式代理端点测试明细</div>';
        }
        return items.map((item) => `
            <div class="playground-info-row">
                <div class="playground-info-label">${escapeHtml(item.endpoint_label || item.endpoint_path || "-")}</div>
                <div class="playground-info-value">
                    ${item.success ? '<span class="playground-status-success">成功</span>' : '<span class="playground-status-danger">失败</span>'}
                    · ${escapeHtml(item.support_label || formatEndpointSupportMode(item.support_mode))}
                    · 状态码 ${escapeHtml(String(item.status_code ?? "-"))}
                    · 耗时 ${escapeHtml(String(item.latency_ms ?? "-"))} ms
                    · ${escapeHtml(item.message || "-")}
                </div>
            </div>
        `).join("");
    }

    function formatEndpointSupportMode(mode) {
        if (mode === "native") return "原生支持";
        if (mode === "adapted") return "通过适配支持";
        if (mode === "unsupported") return "不支持";
        return "支持状态未知";
    }

    function renderProviderTestModalBody(result, options = {}) {
        const scope = options.scope || "provider";
        const titleName = options.name || result?.provider_name || result?.model_name || "测试对象";
        const modelResults = Array.isArray(result?.model_results) ? result.model_results : [];
        const endpointResults = Array.isArray(result?.endpoint_results) ? result.endpoint_results : [];
        const summaryRows = [
            ["测试对象", titleName],
            ["测试范围", scope === "model" ? "单模型测试" : "中转站测试"],
            ["结果", result?.success ? "成功" : "失败"],
            ["健康状态", formatHealthStatusLabel(result?.health_status || "unknown")],
            ["状态码", result?.status_code ?? "-"],
            ["耗时", `${result?.latency_ms ?? "-"} ms`],
        ];
        if (scope === "provider") {
            summaryRows.push(["连通性", result?.provider_success ? "成功" : "失败"]);
            summaryRows.push(["模型通过", `${result?.models_success ?? 0}/${result?.models_total ?? 0}`]);
        }
        const summaryHtml = summaryRows.map(([label, value]) => `
            <div class="provider-test-summary-item">
                <span>${escapeHtml(String(label))}</span>
                <strong>${escapeHtml(String(value))}</strong>
            </div>
        `).join("");
        const messageHtml = escapeHtml(result?.message || "无补充说明");
        const modelHtml = scope === "provider"
            ? (modelResults.length
                ? modelResults.map((item) => `
                    <article class="provider-test-model-item">
                        <div class="provider-test-model-top">
                            <strong>${escapeHtml(item.model_name || "-")}</strong>
                            <div>${statusBadge(item.health_status || "unknown")}</div>
                        </div>
                        <div class="table-muted">状态码 ${item.status_code ?? "-"} · 耗时 ${item.latency_ms ?? "-"} ms</div>
                        <div class="provider-test-model-message">${escapeHtml(item.message || "-")}</div>
                        <div class="provider-test-model-message">${renderEndpointProbeHtml(item.endpoint_results)}</div>
                    </article>
                `).join("")
                : '<div class="empty-state">当前中转站没有可展示的模型测试结果</div>')
            : "";
        return `
            <div class="provider-test-result-shell">
                <section class="provider-test-result-card">
                    <div class="panel-kicker">测试摘要</div>
                    <div class="provider-test-summary-grid">${summaryHtml}</div>
                </section>
                <section class="provider-test-result-card">
                    <div class="panel-kicker">结果说明</div>
                    <div class="provider-test-message">${messageHtml}</div>
                </section>
                <section class="provider-test-result-card">
                    <div class="panel-kicker">端点结果</div>
                    <div class="provider-test-model-message">${renderEndpointProbeHtml(endpointResults)}</div>
                </section>
                ${scope === "provider" ? `
                <section class="provider-test-result-card">
                    <div class="panel-kicker">模型结果</div>
                    <div class="provider-test-model-list">${modelHtml}</div>
                </section>
                ` : ""}
            </div>
        `;
    }

    function createHealthCheckStreamState(scope, title) {
        return {
            scope,
            title,
            running: true,
            currentProviderName: "",
            currentStageLabel: "准备开始",
            stages: [],
            providerSummaries: [],
            finalResult: null,
            errorMessage: "",
        };
    }

    function applyHealthCheckStreamEvent(state, event) {
        if (event.event === "provider_started") {
            state.currentProviderName = event.provider_name || "";
            state.currentStageLabel = `正在准备 ${event.provider_name || "当前中转站"} 的健康检查`;
            return;
        }
        if (event.event === "stage_started") {
            state.currentProviderName = event.provider_name || state.currentProviderName;
            state.currentStageLabel = `${event.provider_name || "当前中转站"} · ${event.phase_label || event.phase_key || "检查中"}`;
            return;
        }
        if (event.event === "stage_completed") {
            const successCount = Number(event.success_count || 0);
            const failureCount = Number(event.failure_count || 0);
            state.stages.push({
                providerName: event.provider_name || state.currentProviderName || "当前中转站",
                phaseLabel: event.phase_label || event.phase_key || "检查阶段",
                skipped: event.skipped === true,
                modelTotal: Number(event.model_total || 0),
                successCount,
                failureCount,
            });
            state.currentStageLabel = event.skipped
                ? `${event.provider_name || state.currentProviderName || "当前中转站"} · ${event.phase_label || "检查阶段"}已跳过`
                : `${event.provider_name || state.currentProviderName || "当前中转站"} · ${event.phase_label || "检查阶段"}已完成`;
            return;
        }
        if (event.event === "provider_completed") {
            state.providerSummaries.push({
                providerName: event.provider_name || "当前中转站",
                success: event.success === true,
                modelsTotal: Number(event.models_total || 0),
                modelsSuccess: Number(event.models_success || 0),
                modelsFailed: Number(event.models_failed || 0),
            });
            return;
        }
        if (event.event === "completed") {
            state.running = false;
            state.finalResult = event.result;
            state.currentStageLabel = "全部检查已完成";
            return;
        }
        if (event.event === "error") {
            state.running = false;
            state.errorMessage = event.message || "健康检查执行失败";
        }
    }

    function renderHealthCheckStageList(stages = []) {
        if (!stages.length) {
            return '<div class="empty-state">当前还没有完成的阶段结果。</div>';
        }
        return stages.map((item) => `
            <article class="provider-test-model-item">
                <div class="provider-test-model-top">
                    <strong>${escapeHtml(item.providerName)}</strong>
                    <div>${item.skipped ? statusBadge("unknown") : statusBadge(item.failureCount > 0 ? "degraded" : "healthy")}</div>
                </div>
                <div class="table-muted">${escapeHtml(item.phaseLabel)}</div>
                <div class="provider-test-model-message">
                    ${item.skipped
                        ? "当前阶段无可执行模型，已跳过。"
                        : `共 ${formatNumber(item.modelTotal)} 个模型，成功 ${formatNumber(item.successCount)} 个，失败 ${formatNumber(item.failureCount)} 个。`}
                </div>
            </article>
        `).join("");
    }

    function renderHealthCheckProviderSummaryList(items = []) {
        if (!items.length) {
            return '<div class="empty-state">当前还没有可展示的中转站结果。</div>';
        }
        return items.map((item) => `
            <article class="provider-test-model-item">
                <div class="provider-test-model-top">
                    <strong>${escapeHtml(item.providerName)}</strong>
                    <div>${statusBadge(item.success ? "healthy" : (item.modelsSuccess > 0 ? "degraded" : "unhealthy"))}</div>
                </div>
                <div class="provider-test-model-message">
                    模型通过 ${formatNumber(item.modelsSuccess)}/${formatNumber(item.modelsTotal)}，失败 ${formatNumber(item.modelsFailed)} 个。
                </div>
            </article>
        `).join("");
    }

    function renderHealthCheckStreamModalBody(state) {
        const providerResultItems = Array.isArray(state.finalResult)
            ? state.finalResult.filter((item) => item.scope === "provider")
            : [];
        const successCount = providerResultItems.filter((item) => item.success).length;
        const totalCount = providerResultItems.length;
        const summaryRows = [
            ["检查范围", state.scope === "all" ? "全部中转站" : "单中转站"],
            ["执行状态", state.errorMessage ? "失败" : (state.running ? "执行中" : "已完成")],
            ["当前阶段", state.currentStageLabel || "准备开始"],
        ];
        if (state.scope === "all" && totalCount > 0) {
            summaryRows.push(["中转站通过", `${formatNumber(successCount)}/${formatNumber(totalCount)}`]);
        }
        const summaryHtml = summaryRows.map(([label, value]) => `
            <div class="provider-test-summary-item">
                <span>${escapeHtml(String(label))}</span>
                <strong>${escapeHtml(String(value))}</strong>
            </div>
        `).join("");
        const progressHtml = `
            <div class="provider-test-result-shell">
                <section class="provider-test-result-card">
                    <div class="panel-kicker">执行进度</div>
                    <div class="provider-test-summary-grid">${summaryHtml}</div>
                </section>
                <section class="provider-test-result-card">
                    <div class="panel-kicker">阶段反馈</div>
                    <div class="provider-test-model-list">${renderHealthCheckStageList(state.stages)}</div>
                </section>
                ${state.scope === "all" ? `
                <section class="provider-test-result-card">
                    <div class="panel-kicker">中转站结果</div>
                    <div class="provider-test-model-list">${renderHealthCheckProviderSummaryList(
                        state.providerSummaries.length ? state.providerSummaries : providerResultItems.map((item) => ({
                            providerName: item.provider_name || "-",
                            success: item.success === true,
                            modelsTotal: Number(item.models_total || 0),
                            modelsSuccess: Number(item.models_success || 0),
                            modelsFailed: Number(item.models_failed || 0),
                        }))
                    )}</div>
                </section>
                ` : ""}
            </div>
        `;
        if (state.errorMessage) {
            return `${progressHtml}
                <section class="provider-test-result-card">
                    <div class="panel-kicker">错误信息</div>
                    <div class="provider-test-message">${escapeHtml(state.errorMessage)}</div>
                </section>
            `;
        }
        if (state.scope === "provider" && state.finalResult && !Array.isArray(state.finalResult)) {
            return `${progressHtml}${renderProviderTestModalBody(state.finalResult, { scope: "provider", name: state.title })}`;
        }
        return progressHtml;
    }

    async function runHealthCheckStream(options) {
        const {
            url,
            title,
            scope,
            trigger,
            showModal = true,
            onCompleted = null,
        } = options;
        const state = createHealthCheckStreamState(scope, title);
        if (showModal) {
            openHealthCheckResultModal(title, renderHealthCheckStreamModalBody(state), trigger);
        }
        await streamJsonLines(url, {}, (event) => {
            applyHealthCheckStreamEvent(state, event);
            if (showModal) {
                openHealthCheckResultModal(title, renderHealthCheckStreamModalBody(state), trigger);
            }
        });
        if (state.errorMessage) {
            throw new Error(state.errorMessage);
        }
        if (typeof onCompleted === "function") {
            await onCompleted(state.finalResult);
        }
        return state.finalResult;
    }

    function setBatchPlaceholder(message) {
        const empty = document.getElementById("playground-batch-output-empty");
        const output = document.getElementById("playground-batch-output");
        if (!empty || !output) return;
        empty.textContent = message;
        empty.classList.remove("hidden");
        output.classList.add("hidden");
        output.innerHTML = "";
    }

    function showBatchRendered(html) {
        const empty = document.getElementById("playground-batch-output-empty");
        const output = document.getElementById("playground-batch-output");
        if (!empty || !output) return;
        empty.classList.add("hidden");
        output.classList.remove("hidden");
        output.innerHTML = html;
    }

    const PRICE_PER_1M_MULTIPLIER = 1000;

    function toPricePer1M(value) {
        if (value == null || Number.isNaN(Number(value))) return null;
        return Number(value) * PRICE_PER_1M_MULTIPLIER;
    }

    function toPricePer1K(value) {
        if (value == null || Number.isNaN(Number(value))) return null;
        return Number(value) / PRICE_PER_1M_MULTIPLIER;
    }

    function collectConfiguredModels(providers = [], options = {}) {
        const {
            requireStream = false,
            requireVision = false,
            allowedModelNameSet = null,
        } = options;
        const seen = new Set();
        const models = [];
        providers
            .filter((provider) => provider.enabled)
            .forEach((provider) => {
                (provider.model_configs || []).forEach((modelConfig) => {
                    if (!modelConfig?.enabled || !modelConfig.model_name) return;
                    if (allowedModelNameSet && !allowedModelNameSet.has(modelConfig.model_name)) return;
                    if (requireStream && !modelConfig.supports_stream) return;
                    if (requireVision && !modelConfig.supports_vision) return;
                    if (seen.has(modelConfig.model_name)) return;
                    seen.add(modelConfig.model_name);
                    models.push(modelConfig.model_name);
                });
            });
        return models.sort((a, b) => a.localeCompare(b, "zh-CN"));
    }

    function collectProviderConfiguredModels(provider, options = {}) {
        const {
            requireStream = false,
            requireVision = false,
            allowedModelNameSet = null,
        } = options;
        if (!provider?.enabled) return [];
        return (provider.model_configs || [])
            .filter((modelConfig) => (
                modelConfig?.enabled
                && modelConfig.model_name
                && (!allowedModelNameSet || allowedModelNameSet.has(modelConfig.model_name))
                && (!requireStream || modelConfig.supports_stream)
                && (!requireVision || modelConfig.supports_vision)
            ))
            .map((modelConfig) => modelConfig.model_name)
            .sort((a, b) => a.localeCompare(b, "zh-CN"));
    }

    function summarizeProviders(providers = []) {
        const providerCount = providers.length;
        const enabledProviderCount = providers.filter((provider) => provider.enabled).length;
        const modelConfigs = providers.flatMap((provider) => provider.model_configs || []);
        const modelCount = modelConfigs.length;
        const healthyProviderCount = providers.filter((provider) => provider.health_status === "healthy").length;
        const degradedProviderCount = providers.filter((provider) => provider.health_status === "degraded").length;
        const unhealthyProviderCount = providers.filter((provider) => provider.health_status === "unhealthy").length;
        const streamModelCount = modelConfigs.filter((model) => model.supports_stream).length;
        const visionModelCount = modelConfigs.filter((model) => model.supports_vision).length;
        const pricedModelCount = modelConfigs.filter((model) => model.input_price_per_1k != null || model.output_price_per_1k != null).length;
        const healthyModelCount = modelConfigs.filter((model) => model.health_status === "healthy").length;
        const unhealthyModelCount = modelConfigs.filter((model) => model.health_status === "unhealthy").length;
        const stabilityModels = modelConfigs.filter((model) => Number.isFinite(Number(model.stability_score)));
        const avgStabilityScore = stabilityModels.length
            ? Number((stabilityModels.reduce((sum, model) => sum + Number(model.stability_score || 0), 0) / stabilityModels.length).toFixed(2))
            : 0;
        return {
            providerCount,
            enabledProviderCount,
            modelCount,
            healthyProviderCount,
            degradedProviderCount,
            unhealthyProviderCount,
            streamModelCount,
            visionModelCount,
            pricedModelCount,
            healthyModelCount,
            unhealthyModelCount,
            avgStabilityScore,
        };
    }

    function getDefaultProviderLabel(providers = [], providerId) {
        if (!providerId) return "未设置默认中转站";
        const provider = providers.find((item) => item.id === Number(providerId));
        return provider ? provider.name : `ID ${providerId}`;
    }

    function formatMappedLabel(mapping, value, fallback = "-") {
        if (value == null || value === "") return fallback;
        return mapping[String(value)] || String(value);
    }

    function formatRouteModeLabel(value) {
        return formatMappedLabel(ROUTE_MODE_LABELS, value);
    }

    function formatLogTypeLabel(value) {
        return formatMappedLabel(LOG_TYPE_LABELS, value);
    }

    function formatHealthStatusLabel(value) {
        return formatMappedLabel(HEALTH_STATUS_LABELS, value);
    }

    function formatCircuitStateLabel(value) {
        return formatMappedLabel(CIRCUIT_STATE_LABELS, value);
    }

    function formatApiClientAuthResultLabel(value) {
        return formatMappedLabel(API_CLIENT_AUTH_RESULT_LABELS, value);
    }

    function formatApiKeyStatusLabel(value) {
        return formatMappedLabel(API_KEY_STATUS_LABELS, value);
    }

    function formatSwitchText(value, enabledLabel = "开启", disabledLabel = "关闭") {
        return value ? enabledLabel : disabledLabel;
    }

    function formatMoney(value) {
        if (value == null || Number.isNaN(Number(value))) return "不限";
        return formatAdaptiveDecimal(value, { maxDecimals: 9 });
    }

    function formatPrice(value) {
        if (value == null || Number.isNaN(Number(value))) return "-";
        return `${toPricePer1M(value).toFixed(4)}/1M`;
    }

    function formatMultiplier(value) {
        if (value == null || Number.isNaN(Number(value))) return "1x";
        return `${Number(value).toFixed(2)}x`;
    }

    function toFiniteNumber(value) {
        const numeric = Number(value);
        return Number.isFinite(numeric) ? numeric : null;
    }

    function trimTrailingZeros(value) {
        return String(value).replace(/(?:\.0+|(\.\d*?[1-9])0+)$/, "$1");
    }

    function formatAdaptiveDecimal(value, options = {}) {
        const {
            minDecimals = 0,
            maxDecimals = 9,
            fallback = "-",
        } = options;
        const numeric = toFiniteNumber(value);
        if (numeric == null) return fallback;
        const absolute = Math.abs(numeric);
        if (absolute === 0) {
            return Number(0).toFixed(minDecimals);
        }
        let decimals = maxDecimals;
        if (absolute >= 1000) {
            decimals = Math.min(maxDecimals, Math.max(minDecimals, 2));
        } else if (absolute >= 1) {
            decimals = Math.min(maxDecimals, Math.max(minDecimals, 6));
        }
        const fixed = numeric.toFixed(decimals);
        return decimals === minDecimals ? fixed : trimTrailingZeros(fixed);
    }

    function roundCurrency(value, decimals = 9) {
        return value == null || Number.isNaN(Number(value)) ? null : Number(Number(value).toFixed(decimals));
    }

    function formatTokenDisplay(value) {
        const numeric = toFiniteNumber(value);
        if (numeric == null) return "-";
        const tokenValue = Math.max(0, numeric);
        if (tokenValue < 1000) {
            return `${formatNumber(Math.round(tokenValue))} tok`;
        }
        const compactValue = tokenValue / 1000;
        const digits = compactValue >= 100 ? 0 : compactValue >= 10 ? 1 : 2;
        return `${trimTrailingZeros(compactValue.toFixed(digits))}k tok`;
    }

    function formatUsdValue(value) {
        const numeric = toFiniteNumber(value);
        if (numeric == null) return "-";
        return `$${formatAdaptiveDecimal(numeric, { maxDecimals: 9, fallback: "0" })}`;
    }

    function formatUsdPer1M(value) {
        const numeric = toFiniteNumber(value);
        if (numeric == null) return "未设置";
        return `$${formatAdaptiveDecimal(numeric * 1000, { maxDecimals: 12, fallback: "0" })}/1M`;
    }

    function sumCosts(...values) {
        const numbers = values.map((value) => toFiniteNumber(value)).filter((value) => value != null);
        if (!numbers.length) return null;
        return roundCurrency(numbers.reduce((sum, value) => sum + value, 0));
    }

    function buildBillingBreakdown(log) {
        const multiplierValue = toFiniteNumber(log?.billing_multiplier);
        const multiplier = multiplierValue != null && multiplierValue > 0 ? multiplierValue : 1;
        const inputPrice = toFiniteNumber(log?.channel_price_input_per_1k);
        const outputPrice = toFiniteNumber(log?.channel_price_output_per_1k);
        const cachePrice = toFiniteNumber(log?.channel_price_cache_per_1k) ?? inputPrice;
        const promptTokens = Math.max(0, toFiniteNumber(log?.prompt_tokens) ?? 0);
        const completionTokens = Math.max(0, toFiniteNumber(log?.completion_tokens) ?? 0);
        const totalTokens = Math.max(0, toFiniteNumber(log?.total_tokens) ?? (promptTokens + completionTokens));
        const cacheReadTokens = Math.max(0, toFiniteNumber(log?.cache_read_tokens) ?? 0);
        const cacheWriteTokens = Math.max(0, toFiniteNumber(log?.cache_write_tokens) ?? 0);
        const regularPromptTokens = Math.max(0, promptTokens - cacheReadTokens);
        const inputCost = inputPrice == null ? null : roundCurrency((regularPromptTokens / 1000) * inputPrice, 9);
        const cacheReadCost = cachePrice == null ? null : roundCurrency((cacheReadTokens / 1000) * cachePrice, 9);
        const outputCost = outputPrice == null ? null : roundCurrency((completionTokens / 1000) * outputPrice, 9);
        const promptCost = roundCurrency(log?.prompt_cost) ?? sumCosts(inputCost, cacheReadCost);
        const completionCost = roundCurrency(log?.completion_cost) ?? outputCost;
        const totalCost = roundCurrency(log?.total_cost) ?? sumCosts(promptCost, completionCost);
        const baseInputPrice = inputPrice == null ? null : roundCurrency(inputPrice / multiplier, 12);
        const baseOutputPrice = outputPrice == null ? null : roundCurrency(outputPrice / multiplier, 12);
        const baseCachePrice = cachePrice == null ? null : roundCurrency(cachePrice / multiplier, 12);
        return {
            multiplier,
            totalTokens,
            promptTokens,
            completionTokens,
            cacheReadTokens,
            cacheWriteTokens,
            regularPromptTokens,
            inputPrice,
            outputPrice,
            cachePrice,
            baseInputPrice,
            baseOutputPrice,
            baseCachePrice,
            inputCost,
            cacheReadCost,
            outputCost,
            promptCost,
            completionCost,
            totalCost,
        };
    }

    function renderBillingTooltip(log) {
        const billing = buildBillingBreakdown(log);
        const originalPriceLines = [
            `输入 ${formatUsdPer1M(billing.baseInputPrice)}`,
            `输出 ${formatUsdPer1M(billing.baseOutputPrice)}`,
            `缓存 ${formatUsdPer1M(billing.baseCachePrice)}`,
        ];
        const channelPriceLines = [
            `输入 ${formatUsdPer1M(billing.inputPrice)}`,
            `输出 ${formatUsdPer1M(billing.outputPrice)}`,
            `缓存 ${formatUsdPer1M(billing.cachePrice)}`,
        ];
        const calculationLines = [
            `输入成本 ${formatTokenDisplay(billing.regularPromptTokens)} × ${formatUsdPer1M(billing.inputPrice)} = ${formatUsdValue(billing.inputCost)}`,
            `缓存读成本 ${formatTokenDisplay(billing.cacheReadTokens)} × ${formatUsdPer1M(billing.cachePrice)} = ${formatUsdValue(billing.cacheReadCost)}`,
            `输出成本 ${formatTokenDisplay(billing.completionTokens)} × ${formatUsdPer1M(billing.outputPrice)} = ${formatUsdValue(billing.outputCost)}`,
            `总成本 = ${formatUsdValue(billing.totalCost)}，倍率 ${formatMultiplier(billing.multiplier)}`,
        ];
        return `
            <div class="log-billing-tooltip-group">
                <strong>原始模型价格</strong>
                ${originalPriceLines.map((line) => `<div>${escapeHtml(line)}</div>`).join("")}
            </div>
            <div class="log-billing-tooltip-group">
                <strong>中转站价格</strong>
                ${channelPriceLines.map((line) => `<div>${escapeHtml(line)}</div>`).join("")}
            </div>
            <div class="log-billing-tooltip-group">
                <strong>计算过程</strong>
                ${calculationLines.map((line) => `<div>${escapeHtml(line)}</div>`).join("")}
            </div>
        `;
    }

    function renderLogBillingCell(log) {
        const billing = buildBillingBreakdown(log);
        return `
            <div class="log-billing-cell">
                <div class="log-billing-row log-billing-row-primary">
                    <div class="log-billing-pair">
                        <span class="log-billing-label">总成本</span>
                        <strong class="log-billing-total-cost">${escapeHtml(formatUsdValue(billing.totalCost))}</strong>
                    </div>
                    <div class="log-billing-pair">
                        <span class="log-billing-label">总 tok</span>
                        <strong class="log-billing-total-tokens">${escapeHtml(formatTokenDisplay(billing.totalTokens))}</strong>
                    </div>
                    <span class="log-billing-help">
                        <button class="log-billing-help-btn" type="button" aria-label="查看价格与计算过程" data-billing-tooltip-trigger="true">
                            <i class="bi bi-question-circle" aria-hidden="true"></i>
                        </button>
                        <div class="log-billing-tooltip-content hidden">
                            ${renderBillingTooltip(log)}
                        </div>
                    </span>
                </div>
                <div class="log-billing-row">
                    <div class="log-billing-pair">
                        <span class="log-billing-label">输入</span>
                        <span class="log-billing-meta">${escapeHtml(formatTokenDisplay(billing.regularPromptTokens))}</span>
                    </div>
                    <div class="log-billing-pair">
                        <span class="log-billing-label">输出</span>
                        <span class="log-billing-meta">${escapeHtml(formatTokenDisplay(billing.completionTokens))}</span>
                    </div>
                </div>
                <div class="log-billing-row">
                    <div class="log-billing-pair">
                        <span class="log-billing-label">缓存读</span>
                        <span class="log-billing-meta">${escapeHtml(formatTokenDisplay(billing.cacheReadTokens))}</span>
                    </div>
                    <div class="log-billing-pair">
                        <span class="log-billing-label">缓存写</span>
                        <span class="log-billing-meta">${escapeHtml(formatTokenDisplay(billing.cacheWriteTokens))}</span>
                    </div>
                </div>
                <div class="log-billing-row log-billing-row-multiplier">
                    <div class="log-billing-pair">
                        <span class="log-billing-label">倍率</span>
                        <span class="log-billing-multiplier">${escapeHtml(formatMultiplier(billing.multiplier))}</span>
                    </div>
                </div>
            </div>
        `;
    }

    function buildLogExceptionReason(log = {}) {
        const detailLines = [];
        const message = String(log.message || "").trim();
        const errorCode = String(log.error_code || "").trim();
        const errorType = String(log.error_type || "").trim();
        const responseText = String(log.response_text || "").trim();
        const billingError = String(log.billing_error || "").trim();
        const tokenFinalizeError = String(log.token_finalize_error || "").trim();
        const authResult = String(log.api_client_auth_result || "").trim();
        if (errorCode) detailLines.push(`错误码：${errorCode}`);
        if (errorType) detailLines.push(`错误类型：${errorType}`);
        if (authResult && authResult !== "authenticated") {
            detailLines.push(`鉴权结果：${formatApiClientAuthResultLabel(authResult)}`);
        }
        if (message && (!log.success || /fail|error|timeout|exceed|invalid/i.test(message))) {
            detailLines.push(`主信息：${message}`);
        }
        if (!detailLines.length && !log.success && responseText) {
            detailLines.push(`响应内容：${responseText}`);
        }
        if (billingError) detailLines.push(`计费补全：${billingError}`);
        if (tokenFinalizeError) detailLines.push(`Token 补全：${tokenFinalizeError}`);
        return Array.from(new Set(detailLines)).filter(Boolean).join("\n");
    }

    function formatLogCellMetricValue(value, suffix = "") {
        if (value == null || Number.isNaN(Number(value))) return "-";
        return `${formatNumber(Number(value))}${suffix}`;
    }

    function renderLogResultCell(log = {}) {
        return `
            <div class="log-result-cell">
                ${renderStatusWithErrorHint(log.success ? "healthy" : "unhealthy", buildLogExceptionReason(log))}
                <div class="table-muted">HTTP ${log.status_code ?? "-"} · 尝试 ${formatLogCellMetricValue(log.attempt_count)}</div>
            </div>
        `;
    }

    function formatBillingCalculation(log) {
        const billing = buildBillingBreakdown(log);
        return [
            `原始价格：输入 ${formatUsdPer1M(billing.baseInputPrice)}，输出 ${formatUsdPer1M(billing.baseOutputPrice)}，缓存 ${formatUsdPer1M(billing.baseCachePrice)}`,
            `中转站价格：输入 ${formatUsdPer1M(billing.inputPrice)}，输出 ${formatUsdPer1M(billing.outputPrice)}，缓存 ${formatUsdPer1M(billing.cachePrice)}`,
            `计算：输入 ${formatTokenDisplay(billing.regularPromptTokens)} -> ${formatUsdValue(billing.inputCost)}；缓存读 ${formatTokenDisplay(billing.cacheReadTokens)} -> ${formatUsdValue(billing.cacheReadCost)}；输出 ${formatTokenDisplay(billing.completionTokens)} -> ${formatUsdValue(billing.outputCost)}`,
            `总成本 ${formatUsdValue(billing.totalCost)}；倍率 ${formatMultiplier(billing.multiplier)}`,
        ].join("；");
    }

    let billingTooltipLayer = null;
    let billingTooltipOwner = null;

    function ensureBillingTooltipLayer() {
        if (billingTooltipLayer) return billingTooltipLayer;
        billingTooltipLayer = document.createElement("div");
        billingTooltipLayer.id = "log-billing-floating-tooltip";
        billingTooltipLayer.className = "log-billing-tooltip log-billing-tooltip-floating hidden";
        billingTooltipLayer.setAttribute("role", "tooltip");
        document.body.appendChild(billingTooltipLayer);
        return billingTooltipLayer;
    }

    function hideBillingTooltip() {
        const tooltip = ensureBillingTooltipLayer();
        tooltip.classList.add("hidden");
        tooltip.innerHTML = "";
        billingTooltipOwner = null;
    }

    function positionBillingTooltip(trigger) {
        const tooltip = ensureBillingTooltipLayer();
        const triggerRect = trigger.getBoundingClientRect();
        const tooltipRect = tooltip.getBoundingClientRect();
        const viewportPadding = 12;
        let top = triggerRect.top - tooltipRect.height - 12;
        let placement = "top";
        if (top < viewportPadding) {
            top = Math.min(window.innerHeight - tooltipRect.height - viewportPadding, triggerRect.bottom + 12);
            placement = "bottom";
        }
        let left = triggerRect.right - tooltipRect.width;
        left = Math.max(viewportPadding, Math.min(left, window.innerWidth - tooltipRect.width - viewportPadding));
        tooltip.style.top = `${top}px`;
        tooltip.style.left = `${left}px`;
        tooltip.dataset.placement = placement;
    }

    function showBillingTooltip(trigger) {
        const wrapper = trigger.closest(".log-billing-help");
        const content = wrapper?.querySelector(".log-billing-tooltip-content");
        if (!content) return;
        const tooltip = ensureBillingTooltipLayer();
        billingTooltipOwner = trigger;
        tooltip.innerHTML = content.innerHTML;
        tooltip.classList.remove("hidden");
        positionBillingTooltip(trigger);
    }

    function initBillingTooltipLayer() {
        if (document.body.dataset.billingTooltipBound === "true") return;
        document.body.dataset.billingTooltipBound = "true";
        document.addEventListener("mouseover", (event) => {
            const trigger = event.target.closest("[data-billing-tooltip-trigger='true']");
            if (!trigger) return;
            if (billingTooltipOwner === trigger) return;
            showBillingTooltip(trigger);
        });
        document.addEventListener("mouseout", (event) => {
            const wrapper = event.target.closest(".log-billing-help");
            if (!wrapper) return;
            if (wrapper.contains(event.relatedTarget)) return;
            hideBillingTooltip();
        });
        document.addEventListener("focusin", (event) => {
            const trigger = event.target.closest("[data-billing-tooltip-trigger='true']");
            if (!trigger) return;
            showBillingTooltip(trigger);
        });
        document.addEventListener("focusout", (event) => {
            const wrapper = event.target.closest(".log-billing-help");
            if (!wrapper) return;
            if (wrapper.contains(event.relatedTarget)) return;
            hideBillingTooltip();
        });
        window.addEventListener("scroll", () => {
            if (billingTooltipOwner) {
                positionBillingTooltip(billingTooltipOwner);
            }
        }, true);
        window.addEventListener("resize", () => {
            if (billingTooltipOwner) {
                positionBillingTooltip(billingTooltipOwner);
            }
        });
    }

    let providerStatusTooltipLayer = null;
    let providerStatusTooltipOwner = null;

    function ensureProviderStatusTooltipLayer() {
        if (providerStatusTooltipLayer) return providerStatusTooltipLayer;
        providerStatusTooltipLayer = document.createElement("div");
        providerStatusTooltipLayer.id = "provider-status-floating-tooltip";
        providerStatusTooltipLayer.className = "provider-status-tooltip provider-status-tooltip-floating hidden";
        providerStatusTooltipLayer.setAttribute("role", "tooltip");
        document.body.appendChild(providerStatusTooltipLayer);
        return providerStatusTooltipLayer;
    }

    function hideProviderStatusTooltip() {
        const tooltip = ensureProviderStatusTooltipLayer();
        tooltip.classList.add("hidden");
        tooltip.innerHTML = "";
        providerStatusTooltipOwner = null;
    }

    function positionProviderStatusTooltip(trigger) {
        const tooltip = ensureProviderStatusTooltipLayer();
        const triggerRect = trigger.getBoundingClientRect();
        const tooltipRect = tooltip.getBoundingClientRect();
        const viewportPadding = 12;
        let top = triggerRect.top - tooltipRect.height - 12;
        let placement = "top";
        if (top < viewportPadding) {
            top = Math.min(window.innerHeight - tooltipRect.height - viewportPadding, triggerRect.bottom + 12);
            placement = "bottom";
        }
        let left = triggerRect.right - tooltipRect.width;
        left = Math.max(viewportPadding, Math.min(left, window.innerWidth - tooltipRect.width - viewportPadding));
        tooltip.style.top = `${top}px`;
        tooltip.style.left = `${left}px`;
        tooltip.dataset.placement = placement;
    }

    function showProviderStatusTooltip(trigger) {
        const wrapper = trigger.closest(".provider-status-help");
        const content = wrapper?.querySelector(".provider-status-tooltip-content");
        if (!content) return;
        const tooltip = ensureProviderStatusTooltipLayer();
        providerStatusTooltipOwner = trigger;
        tooltip.innerHTML = content.innerHTML;
        tooltip.classList.remove("hidden");
        positionProviderStatusTooltip(trigger);
    }

    function initProviderStatusTooltipLayer() {
        if (document.body.dataset.providerStatusTooltipBound === "true") return;
        document.body.dataset.providerStatusTooltipBound = "true";
        document.addEventListener("mouseover", (event) => {
            const trigger = event.target.closest("[data-provider-status-tooltip-trigger='true']");
            if (!trigger) return;
            if (providerStatusTooltipOwner === trigger) return;
            showProviderStatusTooltip(trigger);
        });
        document.addEventListener("mouseout", (event) => {
            const wrapper = event.target.closest(".provider-status-help");
            if (!wrapper) return;
            if (wrapper.contains(event.relatedTarget)) return;
            hideProviderStatusTooltip();
        });
        document.addEventListener("focusin", (event) => {
            const trigger = event.target.closest("[data-provider-status-tooltip-trigger='true']");
            if (!trigger) return;
            showProviderStatusTooltip(trigger);
        });
        document.addEventListener("focusout", (event) => {
            const wrapper = event.target.closest(".provider-status-help");
            if (!wrapper) return;
            if (wrapper.contains(event.relatedTarget)) return;
            hideProviderStatusTooltip();
        });
        window.addEventListener("scroll", () => {
            if (providerStatusTooltipOwner) {
                positionProviderStatusTooltip(providerStatusTooltipOwner);
            }
        }, true);
        window.addEventListener("resize", () => {
            if (providerStatusTooltipOwner) {
                positionProviderStatusTooltip(providerStatusTooltipOwner);
            }
        });
    }

    function formatPercent(value) {
        if (value == null || Number.isNaN(Number(value))) return "-";
        return `${Number(value).toFixed(2)}%`;
    }

    function formatLatencyMs(value) {
        if (value == null || Number.isNaN(Number(value))) return "-";
        return `${Math.round(Number(value))} ms`;
    }

    function formatScore(value) {
        if (value == null || Number.isNaN(Number(value))) return "-";
        return `${Number(value).toFixed(2)} 分`;
    }

    function renderQualitySummary(entity) {
        return `
            <div>成功率 ${escapeHtml(formatPercent(entity.success_rate))}</div>
            <div class="table-muted">首 Token ${escapeHtml(formatLatencyMs(entity.avg_first_token_latency_ms))}</div>
            <div class="table-muted">稳定性 ${escapeHtml(formatScore(entity.stability_score))} · 样本 ${escapeHtml(String(entity.recent_request_count ?? 0))}</div>
        `;
    }

    function formatHealthOverview(healthyCount, degradedCount, unhealthyCount) {
        return `健康 ${healthyCount} / 降级 ${degradedCount} / 异常 ${unhealthyCount}`;
    }

    function formatStatusBadgeLabel(value) {
        if (value in HEALTH_STATUS_LABELS) return formatHealthStatusLabel(value);
        if (value in CIRCUIT_STATE_LABELS) return formatCircuitStateLabel(value);
        if (value in API_KEY_STATUS_LABELS) return formatApiKeyStatusLabel(value);
        return String(value ?? "-");
    }

    function getRouteModeDefinitions(defaultProviderLabel, manualAllowFallback) {
        const manualFallbackText = manualAllowFallback
            ? "默认中转失败后，继续按模型候选池回退。"
            : "默认中转失败后，立即返回失败，不再切换其它中转。";
        return {
            manual: {
                title: formatRouteModeLabel("manual"),
                tag: "强控制",
                summary: "固定把默认中转站当成首选，适合明确指定主线路、灰度验证或内部定向测试。",
                points: [
                    `优先尝试默认中转站：${defaultProviderLabel}。`,
                    manualFallbackText,
                    "如果默认中转没有该模型或该模型当前不可用，是否还能继续分发，取决于 fallback 是否开启。",
                ],
            },
            failover: {
                title: formatRouteModeLabel("failover"),
                tag: "高可用",
                summary: "先尝试默认中转，失败后自动切到其它可用候选，是最稳妥的常规生产模式。",
                points: [
                    `优先尝试默认中转站：${defaultProviderLabel}。`,
                    "默认中转失败后，按模型健康度、优先级、最近成功率和延迟依次切换。",
                    "适合主线路明确，但要求自动兜底的场景。",
                ],
            },
            weighted: {
                title: formatRouteModeLabel("weighted"),
                tag: "自动分流",
                summary: "系统会先按模型级权重做首选分流，再用综合排序做回退，适合多副本共享流量。",
                points: [
                    "首个命中不是固定的，而是根据模型权重和近期失败率动态调整。",
                    "某条线路近期失败率高时，会被自动降权，不会持续吃满流量。",
                    "首选失败后，仍会按健康度、优先级、成功率和延迟继续回退。",
                ],
            },
            sticky: {
                title: formatRouteModeLabel("sticky"),
                tag: "会话稳定",
                summary: "同一用户或同一会话优先落到同一中转，减少多轮对话在不同线路间跳动。",
                points: [
                    "系统会根据用户、会话或对话相关标识生成 sticky key。",
                    "相同 key 会优先命中同一首选中转，但不是绝对锁死，失败时仍会回退。",
                    "适合追求连续多轮对话体验更稳定的场景。",
                ],
            },
        };
    }

    function buildRouteModeCards(routeMode, defaultProviderLabel, manualAllowFallback) {
        const definitions = getRouteModeDefinitions(defaultProviderLabel, manualAllowFallback);
        return Object.entries(definitions).map(([key, item]) => `
            <article class="route-mode-card ${key === routeMode ? "active" : ""}">
                <div class="route-mode-card-header">
                    <strong>${escapeHtml(item.title)}</strong>
                    <span class="route-mode-pill">${escapeHtml(item.tag)}</span>
                </div>
                <div class="route-mode-card-summary">${escapeHtml(item.summary)}</div>
                <ul class="route-mode-card-points">
                    ${item.points.map((point) => `<li>${escapeHtml(point)}</li>`).join("")}
                </ul>
            </article>
        `).join("");
    }

    function buildRouteLiveSummary(routeMode, defaultProviderLabel, manualAllowFallback, hasDefaultProvider) {
        const commonSteps = [
            {
                title: "先按模型过滤候选池",
                text: "只有已启用、支持请求模型、且模型健康状态可参与路由的 provider_model 才会进入候选列表。",
            },
        ];
        const chips = [
            `当前模式：${formatRouteModeLabel(routeMode)}`,
            `默认中转：${defaultProviderLabel}`,
            `失败后回退：${formatSwitchText(manualAllowFallback)}`,
        ];
        let modeSteps = [];
        let warning = "";

        if (routeMode === "manual") {
            modeSteps = [
                {
                    title: "首选固定默认中转",
                    text: hasDefaultProvider
                        ? `系统先尝试默认中转站「${defaultProviderLabel}」。`
                        : "当前没有配置默认中转站，因此不会有固定首选线路。",
                },
                {
                    title: "是否允许继续回退",
                    text: manualAllowFallback
                        ? "已开启 fallback。默认中转失败后，会继续尝试其它模型候选。"
                        : "已关闭 fallback。默认中转失败、缺少该模型或当前不可用时，请求会直接失败。",
                },
            ];
            if (!hasDefaultProvider) {
                warning = "当前是 manual 模式，但默认中转站为空。这样不会形成真正的“固定主线路”，如果同时关闭 fallback，请求很容易直接失败。";
            } else if (!manualAllowFallback) {
                warning = "当前 manual 模式已关闭 fallback。默认中转一旦无该模型、鉴权失败或线路异常，请求不会自动切换到其它中转。";
            }
        } else if (routeMode === "failover") {
            modeSteps = [
                {
                    title: "先打默认中转",
                    text: hasDefaultProvider
                        ? `系统会先尝试默认中转站「${defaultProviderLabel}」。`
                        : "当前没有默认中转站，因此首选阶段会被跳过。",
                },
                {
                    title: "失败后按综合顺序切换",
                    text: "回退顺序取决于模型健康度、模型优先级、provider 优先级、最近成功率和平均延迟。",
                },
            ];
            if (!hasDefaultProvider) {
                warning = "当前 failover 模式未设置默认中转站，系统仍能分发，但会退化为直接按候选排序选择首个可用线路。";
            }
        } else if (routeMode === "weighted") {
            modeSteps = [
                {
                    title: "首选按动态权重分流",
                    text: "首个命中由模型级权重决定，近期失败率越高，动态权重会越低。",
                },
                {
                    title: "失败后继续按排序回退",
                    text: "首选失败时，不会再次随机，而是按照综合评分链路继续尝试其它候选。",
                },
            ];
        } else if (routeMode === "sticky") {
            modeSteps = [
                {
                    title: "同 key 优先同中转",
                    text: "系统会根据 user 或 metadata 中的 session / conversation / thread 标识计算 sticky key。",
                },
                {
                    title: "保持稳定，但不死锁",
                    text: "同一个 key 会优先命中同一首选；如果该线路失败，仍会自动回退到其它候选。",
                },
            ];
        }

        const tailStep = {
            title: "代理层执行真正转发",
            text: "非流式会逐个尝试候选；流式只允许在首包前切换，一旦开始输出就不再中途换线。",
        };

        return `
            <div class="route-live-chip-row">
                ${chips.map((chip) => `<span class="route-live-chip">${escapeHtml(chip)}</span>`).join("")}
            </div>
            <div class="route-live-steps">
                ${commonSteps.concat(modeSteps).concat(tailStep).map((step, index) => `
                    <div class="route-live-step">
                        <div class="route-live-step-index">${index + 1}</div>
                        <div>
                            <strong>${escapeHtml(step.title)}</strong>
                            <span>${escapeHtml(step.text)}</span>
                        </div>
                    </div>
                `).join("")}
            </div>
            ${warning ? `<div class="route-live-warning">${escapeHtml(warning)}</div>` : ""}
        `;
    }

    function renderRoutePolicyGuideInto({
        modeCards,
        liveSummary,
        routeMode,
        defaultProviderId,
        manualAllowFallback,
        providers = [],
    }) {
        if (!modeCards) return;
        const defaultProviderLabel = getDefaultProviderLabel(providers, defaultProviderId);
        const hasDefaultProvider = Boolean(defaultProviderId) && providers.some((item) => item.id === Number(defaultProviderId));
        modeCards.innerHTML = buildRouteModeCards(routeMode, defaultProviderLabel, manualAllowFallback);
        if (liveSummary) {
            liveSummary.innerHTML = buildRouteLiveSummary(routeMode, defaultProviderLabel, manualAllowFallback, hasDefaultProvider);
        }
    }

    function renderRoutePolicyGuide({
        routeMode,
        defaultProviderId,
        manualAllowFallback,
        providers = [],
    }) {
        renderRoutePolicyGuideInto({
            modeCards: document.getElementById("route-mode-cards"),
            liveSummary: document.getElementById("route-live-summary"),
            routeMode,
            defaultProviderId,
            manualAllowFallback,
            providers,
        });
    }

    function renderProviderModelHealth(modelConfigs = [], providerId) {
        if (!modelConfigs.length) return '<span class="table-muted">未挂载模型</span>';
        const visibleModels = modelConfigs.slice(0, 1);
        const enabledCount = modelConfigs.filter((item) => item.enabled).length;
        const healthyCount = modelConfigs.filter((item) => item.health_status === "healthy").length;
        const streamCount = modelConfigs.filter((item) => item.supports_stream).length;
        const visionCount = modelConfigs.filter((item) => item.supports_vision).length;
        const hiddenCount = Math.max(modelConfigs.length - visibleModels.length, 0);
        return `
            <div class="provider-model-summary">
                <div class="provider-model-summary-head">
                    <strong>${formatNumber(modelConfigs.length)} 个模型</strong>
                    <span>${formatNumber(enabledCount)} 启用 · ${formatNumber(healthyCount)} 状态正常 · ${formatNumber(streamCount)} 流式 · ${formatNumber(visionCount)} 图像</span>
                </div>
                <div class="provider-model-preview-list">
                    ${visibleModels.map((item) => `
                        <span class="provider-model-preview-chip">
                            <strong>${escapeHtml(item.model_name)}</strong>
                            <span>${escapeHtml(formatMultiplier(item.price_multiplier ?? 1))}</span>
                        </span>
                    `).join("")}
                    ${hiddenCount ? `<span class="provider-model-more-chip">+${formatNumber(hiddenCount)}</span>` : ""}
                </div>
                ${hiddenCount ? `<button class="table-action-btn provider-model-detail-btn" data-action="view-models" data-id="${providerId}" type="button">查看全部</button>` : ""}
            </div>
        `;
    }

    function escapeHtml(value) {
        return String(value)
            .replaceAll("&", "&amp;")
            .replaceAll("<", "&lt;")
            .replaceAll(">", "&gt;")
            .replaceAll('"', "&quot;")
            .replaceAll("'", "&#39;");
    }

    function formatDate(value) {
        if (!value) return "-";
        let normalized = value;
        if (typeof value === "string") {
            normalized = value.includes("T") ? value : value.replace(" ", "T");
            if (!/[zZ]|[+\-]\d{2}:\d{2}$/.test(normalized)) {
                normalized = `${normalized}Z`;
            }
        }
        const date = new Date(normalized);
        if (Number.isNaN(date.getTime())) return String(value);
        return date.toLocaleString("zh-CN", { hour12: false });
    }

    function formatNumber(value) {
        const normalized = Number(value ?? 0);
        if (!Number.isFinite(normalized)) return "-";
        return normalized.toLocaleString("zh-CN");
    }

    function toDatetimeLocalInputValue(value) {
        if (!value) return "";
        const normalized = typeof value === "string" && !/[zZ]|[+\-]\d{2}:\d{2}$/.test(value)
            ? `${value.replace(" ", "T")}Z`
            : value;
        const date = new Date(normalized);
        if (Number.isNaN(date.getTime())) return "";
        const year = date.getFullYear();
        const month = String(date.getMonth() + 1).padStart(2, "0");
        const day = String(date.getDate()).padStart(2, "0");
        const hours = String(date.getHours()).padStart(2, "0");
        const minutes = String(date.getMinutes()).padStart(2, "0");
        return `${year}-${month}-${day}T${hours}:${minutes}`;
    }

    function statusBadge(value) {
        const normalized = String(value || "unknown");
        return `<span class="status-badge status-${normalized}">${escapeHtml(formatStatusBadgeLabel(normalized))}</span>`;
    }

    function renderStatusWithErrorHint(status, errorMessage) {
        const normalizedError = String(errorMessage || "").trim();
        return `
            <span class="provider-status-cell">
                ${statusBadge(status || "unknown")}
                ${normalizedError ? `
                    <span class="provider-status-help">
                        <button class="provider-status-help-btn" type="button" aria-label="查看状态异常原因" data-provider-status-tooltip-trigger="true">
                            <i class="bi bi-question-circle" aria-hidden="true"></i>
                        </button>
                        <div class="provider-status-tooltip-content hidden">
                            <div class="provider-status-tooltip-title">异常原因</div>
                            <div class="provider-status-tooltip-copy">${escapeHtml(normalizedError)}</div>
                        </div>
                    </span>
                ` : ""}
            </span>
        `;
    }

    function setPlaygroundPlaceholder(message) {
        const empty = document.getElementById("playground-output-empty");
        const output = document.getElementById("playground-output");
        if (!empty || !output) return;
        empty.textContent = message;
        empty.classList.remove("hidden");
        output.classList.add("hidden");
        output.innerHTML = "";
    }

    function showPlaygroundRendered(html) {
        const empty = document.getElementById("playground-output-empty");
        const output = document.getElementById("playground-output");
        if (!empty || !output) return;
        empty.classList.add("hidden");
        output.classList.remove("hidden");
        output.innerHTML = html;
    }

    function renderBatchProviderPicker(providers = []) {
        const picker = document.getElementById("playground-provider-picker");
        if (!picker) return;
        if (!providers.length) {
            picker.innerHTML = '<div class="playground-provider-list-empty">当前还没有已配置渠道</div>';
            return;
        }
        picker.innerHTML = providers.map((provider) => `
            <label class="playground-provider-option">
                <input type="checkbox" value="${provider.id}" data-provider-select ${provider.enabled ? "checked" : ""}>
                <div class="playground-provider-option-copy">
                    <strong>${escapeHtml(provider.name)}</strong>
                    <span>${provider.enabled ? "已启用" : "已停用"} · ${escapeHtml(provider.base_url)} · ${provider.model_configs?.length ?? 0} 个模型</span>
                </div>
                <div>${statusBadge(provider.health_status || "unknown")}</div>
            </label>
        `).join("");
    }

    function updateBatchSelection(providers = [], mode = "enabled") {
        const selectedIds = new Set();
        if (mode === "all") {
            providers.forEach((provider) => selectedIds.add(String(provider.id)));
        } else if (mode === "enabled") {
            providers.filter((provider) => provider.enabled).forEach((provider) => selectedIds.add(String(provider.id)));
        }
        document.querySelectorAll('#playground-provider-picker [data-provider-select]').forEach((input) => {
            input.checked = selectedIds.has(input.value);
        });
    }

    function getSelectedProviderIds() {
        return Array.from(document.querySelectorAll('#playground-provider-picker [data-provider-select]:checked'))
            .map((input) => Number(input.value))
            .filter((value) => Number.isFinite(value));
    }

    function summarizeBatchConnectivityResults(results = []) {
        const providerTotal = results.length;
        const providerSuccess = results.filter((item) => item.success).length;
        const providerFailed = Math.max(providerTotal - providerSuccess, 0);
        const modelTotal = results.reduce((sum, item) => sum + Number(item.models_total ?? 0), 0);
        const modelSuccess = results.reduce((sum, item) => sum + Number(item.models_success ?? 0), 0);
        const modelFailed = Math.max(modelTotal - modelSuccess, 0);
        const totalLatency = results.reduce((sum, item) => sum + Number(item.latency_ms ?? 0), 0);
        const averageLatency = providerTotal ? Math.round(totalLatency / providerTotal) : 0;
        return {
            providerTotal,
            providerSuccess,
            providerFailed,
            modelTotal,
            modelSuccess,
            modelFailed,
            averageLatency,
        };
    }

    function buildBatchConnectivitySearchText(item) {
        const modelText = (item.model_results || []).map((model) => [
            model.model_name || "",
            model.message || "",
            (model.endpoint_results || []).map((endpoint) => endpoint.endpoint_label || endpoint.endpoint_path || "").join(" "),
        ].join(" ")).join(" ");
        return [
            item.provider_name || "",
            item.message || "",
            item.status_code ?? "",
            item.health_status || "",
            modelText,
        ].join(" ").toLowerCase();
    }

    function summarizeEndpointResults(endpointResults = []) {
        if (!Array.isArray(endpointResults) || !endpointResults.length) {
            return "-";
        }
        return endpointResults
            .map((item) => `${item.support_label || `${item.endpoint_label || item.endpoint_path || "-"}${item.success ? "成功" : "失败"}`}`)
            .join(" / ");
    }

    function renderBatchConnectivityResults(results = [], options = {}) {
        if (!results.length) {
            return `
                <section class="playground-result-card playground-result-error">
                    <div class="playground-card-title">批量测试结果</div>
                    <div class="playground-reply-content"><p>没有可展示的测试结果。</p></div>
                </section>
            `;
        }

        const keyword = String(options.keyword || "").trim().toLowerCase();
        const health = String(options.health || "");
        const pageSize = Math.max(1, Number(options.pageSize || 10));
        const summary = summarizeBatchConnectivityResults(results);
        const filteredResults = results.filter((item) => {
            const keywordMatched = !keyword || buildBatchConnectivitySearchText(item).includes(keyword);
            if (!keywordMatched) {
                return false;
            }
            if (!health) {
                return true;
            }
            if (health === "healthy") {
                return item.success && (item.health_status || "healthy") === "healthy";
            }
            if (health === "abnormal") {
                return !item.success || (item.health_status || "unknown") !== "healthy";
            }
            return (item.health_status || "unknown") === health;
        });
        const totalPages = Math.max(1, Math.ceil(filteredResults.length / pageSize));
        const currentPage = Math.min(Math.max(1, Number(options.page || 1)), totalPages);
        const pageStart = (currentPage - 1) * pageSize;
        const pageItems = filteredResults.slice(pageStart, pageStart + pageSize);

        return `
            <section class="playground-result-card">
                <div class="playground-batch-card-head">
                    <div>
                        <div class="playground-card-title">批量测试概览</div>
                        <div class="table-muted">本次共测试 ${summary.providerTotal} 个渠道、${summary.modelTotal} 个模型；当前筛选命中 ${filteredResults.length} 个渠道。</div>
                    </div>
                    <div>${statusBadge(summary.providerFailed ? "warning" : "healthy")}</div>
                </div>
                <div class="playground-batch-summary-grid">
                    <article class="playground-batch-summary-item">
                        <span>渠道总数</span>
                        <strong>${summary.providerTotal}</strong>
                    </article>
                    <article class="playground-batch-summary-item">
                        <span>结果正常</span>
                        <strong>${summary.providerSuccess}</strong>
                    </article>
                    <article class="playground-batch-summary-item">
                        <span>结果异常</span>
                        <strong>${summary.providerFailed}</strong>
                    </article>
                    <article class="playground-batch-summary-item">
                        <span>模型总数</span>
                        <strong>${summary.modelTotal}</strong>
                    </article>
                    <article class="playground-batch-summary-item">
                        <span>模型成功</span>
                        <strong>${summary.modelSuccess}</strong>
                    </article>
                    <article class="playground-batch-summary-item">
                        <span>平均耗时</span>
                        <strong>${summary.averageLatency} ms</strong>
                    </article>
                </div>
            </section>
            <section class="playground-result-card">
                <div class="playground-batch-card-head">
                    <div>
                        <div class="playground-card-title">批量测试具体结果</div>
                        <div class="table-muted">按渠道列表分页查看，并支持关键词与健康状态筛选。</div>
                    </div>
                </div>
                <div class="filter-toolbar playground-batch-filter-toolbar">
                    <label>
                        <span>关键词</span>
                        <input class="field-input search-input" id="playground-batch-search" type="text" value="${escapeHtml(options.keyword || "")}" placeholder="搜索渠道名、模型名、结果说明">
                    </label>
                    <label>
                        <span>健康状态</span>
                        <select class="field-input" id="playground-batch-health">
                            <option value="" ${health === "" ? "selected" : ""}>全部</option>
                            <option value="healthy" ${health === "healthy" ? "selected" : ""}>健康</option>
                            <option value="abnormal" ${health === "abnormal" ? "selected" : ""}>异常</option>
                        </select>
                    </label>
                </div>
                <div class="playground-batch-result-list">
                    ${pageItems.length ? pageItems.map((item) => `
                        <article class="playground-batch-list-item">
                            <div class="playground-batch-card-head">
                                <div>
                                    <div class="playground-card-title">${escapeHtml(item.provider_name || `渠道 ${item.provider_id}`)}</div>
                                    <div class="table-muted">${item.provider_enabled ? "已启用" : "已停用"} · 模型 ${item.models_success ?? 0}/${item.models_total ?? 0} 正常</div>
                                </div>
                                <div>${statusBadge(item.health_status || "unknown")}</div>
                            </div>
                            <div class="playground-batch-provider-grid">
                                <div><span>渠道连通</span><strong class="${item.provider_success ? "playground-status-success" : "playground-status-danger"}">${item.provider_success ? "成功" : "失败"}</strong></div>
                                <div><span>状态码</span><strong>${item.status_code ?? "-"}</strong></div>
                                <div><span>耗时</span><strong>${item.latency_ms ?? "-"} ms</strong></div>
                                <div><span>模型健康</span><strong>${item.models_success ?? 0}/${item.models_total ?? 0}</strong></div>
                            </div>
                            <div class="playground-batch-provider-note">${escapeHtml(item.message || "-")}</div>
                            <div class="playground-batch-model-table">
                                <div class="playground-batch-model-table-head">
                                    <span>模型</span>
                                    <span>健康</span>
                                    <span>状态 / 耗时</span>
                                    <span>端点探测</span>
                                    <span>结果说明</span>
                                </div>
                                ${(item.model_results || []).length ? item.model_results.map((model) => `
                                    <div class="playground-batch-model-row">
                                        <strong>${escapeHtml(model.model_name || "-")}</strong>
                                        <div>${statusBadge(model.health_status || "unknown")}</div>
                                        <span>状态码 ${model.status_code ?? "-"} · ${model.latency_ms ?? "-"} ms</span>
                                        <span>${escapeHtml(summarizeEndpointResults(model.endpoint_results))}</span>
                                        <span>${escapeHtml(model.message || "-")}</span>
                                    </div>
                                `).join("") : '<div class="playground-provider-list-empty">当前渠道没有可测试模型</div>'}
                            </div>
                        </article>
                    `).join("") : `
                        <div class="playground-provider-list-empty">当前筛选条件下没有命中的测试结果，请调整关键词或健康状态。</div>
                    `}
                </div>
                <div class="table-toolbar logs-pagination-bar playground-batch-pagination-bar">
                    <label class="logs-page-size">
                        <span>每页数量</span>
                        <select class="field-input" id="playground-batch-page-size">
                            <option value="5" ${pageSize === 5 ? "selected" : ""}>5</option>
                            <option value="10" ${pageSize === 10 ? "selected" : ""}>10</option>
                            <option value="20" ${pageSize === 20 ? "selected" : ""}>20</option>
                            <option value="50" ${pageSize === 50 ? "selected" : ""}>50</option>
                        </select>
                    </label>
                    <div class="logs-page-meta">第 ${currentPage} / ${totalPages} 页，共 ${filteredResults.length} 条</div>
                    <div class="logs-page-actions">
                        <button class="btn btn-ghost interactive-btn" id="playground-batch-prev-page" type="button" ${currentPage <= 1 ? "disabled" : ""}>上一页</button>
                        <button class="btn btn-ghost interactive-btn" id="playground-batch-next-page" type="button" ${currentPage >= totalPages ? "disabled" : ""}>下一页</button>
                    </div>
                </div>
            </section>
        `;
    }

    function validateRawApiKeyFormat(value, { allowEmpty = false } = {}) {
        const rawValue = String(value || "").trim();
        if (!rawValue) {
            return allowEmpty
                ? { valid: true, empty: true, message: "留空时将自动生成或保留当前密钥" }
                : { valid: false, empty: true, message: "请填写自定义 API 密钥" };
        }
        if (rawValue.length < API_KEY_RAW_MIN_LENGTH) {
            return { valid: false, empty: false, message: `长度至少 ${API_KEY_RAW_MIN_LENGTH} 位` };
        }
        if (rawValue.length > API_KEY_RAW_MAX_LENGTH) {
            return { valid: false, empty: false, message: `长度不能超过 ${API_KEY_RAW_MAX_LENGTH} 位` };
        }
        if (!rawValue.startsWith(API_KEY_RAW_PREFIX)) {
            return { valid: false, empty: false, message: `必须以 ${API_KEY_RAW_PREFIX} 开头` };
        }
        if (!API_KEY_RAW_PATTERN.test(rawValue)) {
            return { valid: false, empty: false, message: "仅允许字母、数字、连字符和下划线" };
        }
        return { valid: true, empty: false, message: "密钥格式符合要求，可以创建" };
    }

    function updateRawApiKeyValidationState(input, feedback, options = {}) {
        if (!input || !feedback) return { valid: true, empty: true, message: "" };
        const result = validateRawApiKeyFormat(input.value, options);
        feedback.classList.remove("is-valid", "is-invalid", "is-neutral");
        input.classList.remove("is-valid", "is-invalid");
        if (result.empty && options.allowEmpty) {
            feedback.classList.add("is-neutral");
            feedback.textContent = result.message;
            input.removeAttribute("aria-invalid");
            return result;
        }
        feedback.classList.add(result.valid ? "is-valid" : "is-invalid");
        feedback.textContent = result.message;
        input.classList.add(result.valid ? "is-valid" : "is-invalid");
        input.setAttribute("aria-invalid", result.valid ? "false" : "true");
        return result;
    }

    function selectTextForClipboard(value) {
        const textarea = document.createElement("textarea");
        textarea.value = value;
        textarea.setAttribute("readonly", "");
        textarea.style.position = "fixed";
        textarea.style.left = "-9999px";
        textarea.style.top = "0";
        textarea.style.opacity = "0";
        document.body.appendChild(textarea);
        textarea.focus({ preventScroll: true });
        textarea.select();
        textarea.setSelectionRange(0, textarea.value.length);
        return textarea;
    }

    async function writeClipboardText(value) {
        const text = String(value || "");
        if (!text) return false;
        try {
            if (navigator.clipboard && window.isSecureContext) {
                await navigator.clipboard.writeText(text);
                return true;
            }
        } catch {
            // Fall through to the legacy selection-based path for http deployments.
        }
        const textarea = selectTextForClipboard(text);
        try {
            return document.execCommand("copy");
        } finally {
            textarea.remove();
        }
    }

    function setCopyButtonFeedback(button, status) {
        if (!button) return;
        if (!button.dataset.copyOriginalText) {
            button.dataset.copyOriginalText = button.textContent.trim() || "复制";
        }
        window.clearTimeout(Number(button.dataset.copyFeedbackTimer || 0));
        button.classList.remove("is-copying", "is-copy-success", "is-copy-error");
        if (status === "copying") {
            button.classList.add("is-copying", "is-pressed");
            button.textContent = "复制中";
            button.disabled = true;
            return;
        }
        if (status === "success") {
            button.classList.add("is-copy-success");
            button.textContent = "已复制";
        } else if (status === "error") {
            button.classList.add("is-copy-error");
            button.textContent = "复制失败";
        } else {
            button.textContent = button.dataset.copyOriginalText || "复制";
        }
        button.disabled = false;
        button.dataset.copyFeedbackTimer = String(window.setTimeout(() => {
            button.classList.remove("is-copying", "is-copy-success", "is-copy-error", "is-pressed");
            button.textContent = button.dataset.copyOriginalText || "复制";
            button.disabled = false;
        }, status === "error" ? 1800 : 1400));
    }

    async function copyText(value, button = null) {
        if (!value) return false;
        setCopyButtonFeedback(button, "copying");
        try {
            const copied = await writeClipboardText(value);
            if (!copied) throw new Error("copy command returned false");
            setCopyButtonFeedback(button, "success");
            showToast("已复制");
            return true;
        } catch {
            setCopyButtonFeedback(button, "error");
            showToast("复制失败", "error");
            return false;
        }
    }

    function initRawApiKeyValidationControls(scope = document) {
        scope.querySelectorAll("[data-raw-api-key-input]").forEach((input) => {
            if (input.dataset.rawApiKeyValidationBound === "true") return;
            input.dataset.rawApiKeyValidationBound = "true";
            const form = input.closest("form");
            const feedback = form?.querySelector("[data-raw-api-key-feedback]");
            const submitButton = form?.querySelector('button[type="submit"]');
            const allowEmpty = input.dataset.rawApiKeyOptional !== "false";

            const refresh = () => {
                const result = updateRawApiKeyValidationState(input, feedback, { allowEmpty });
                if (submitButton) {
                    submitButton.disabled = !result.valid;
                }
                return result;
            };

            input.addEventListener("input", refresh);
            input.addEventListener("blur", refresh);
            form?.addEventListener("submit", (event) => {
                const result = refresh();
                if (!result.valid) {
                    event.preventDefault();
                    input.focus();
                    showToast(result.message, "error");
                }
            });
            refresh();
        });
    }

    function formatCodeValue(value) {
        if (value == null || value === "") return "-";
        if (typeof value === "string") {
            const parsed = safeJsonParse(value);
            if (parsed !== null) {
                return JSON.stringify(parsed, null, 2);
            }
            return value;
        }
        return JSON.stringify(value, null, 2);
    }

    function renderTrendChart(container) {
        if (!container) return;
        const trend = safeJsonParse(container.dataset.trend || "[]");
        if (!Array.isArray(trend) || !trend.length) {
            container.innerHTML = '<div class="empty-state">暂无趋势数据</div>';
            return;
        }
        const width = 560;
        const height = 220;
        const paddingX = 28;
        const paddingTop = 18;
        const paddingBottom = 40;
        const chartHeight = height - paddingTop - paddingBottom;
        const chartWidth = width - paddingX * 2;
        const maxRequests = Math.max(1, ...trend.map((item) => Number(item.requests || 0)));
        const maxCost = Math.max(1, ...trend.map((item) => Number(item.cost || 0)));
        const step = trend.length > 1 ? chartWidth / (trend.length - 1) : chartWidth;
        const barWidth = Math.max(18, Math.min(44, chartWidth / Math.max(trend.length * 1.4, 1)));
        const linePoints = trend.map((item, index) => {
            const x = paddingX + index * step;
            const y = paddingTop + chartHeight - (Number(item.cost || 0) / maxCost) * chartHeight;
            return `${x},${y}`;
        }).join(" ");
        const bars = trend.map((item, index) => {
            const x = paddingX + index * step - barWidth / 2;
            const barHeight = (Number(item.requests || 0) / maxRequests) * chartHeight;
            const y = paddingTop + chartHeight - barHeight;
            return `<rect x="${x.toFixed(2)}" y="${y.toFixed(2)}" width="${barWidth.toFixed(2)}" height="${Math.max(barHeight, 4).toFixed(2)}" rx="10" fill="rgba(24, 119, 242, 0.18)" stroke="rgba(24, 119, 242, 0.6)"></rect>`;
        }).join("");
        const labels = trend.map((item, index) => {
            const x = paddingX + index * step;
            return `<text x="${x.toFixed(2)}" y="${(height - 12).toFixed(2)}" text-anchor="middle" fill="#6b7280" font-size="12">${escapeHtml(item.label || "-")}</text>`;
        }).join("");
        const nodes = trend.map((item, index) => {
            const x = paddingX + index * step;
            const y = paddingTop + chartHeight - (Number(item.cost || 0) / maxCost) * chartHeight;
            return `<circle cx="${x.toFixed(2)}" cy="${y.toFixed(2)}" r="4" fill="#0f172a"></circle>`;
        }).join("");
        const totalRequests = trend.reduce((sum, item) => sum + Number(item.requests || 0), 0);
        const totalTokens = trend.reduce((sum, item) => sum + Number(item.tokens || 0), 0);
        const totalCost = trend.reduce((sum, item) => sum + Number(item.cost || 0), 0);
        container.innerHTML = `
            <div class="dashboard-signal-list">
                <div><span>总请求</span><strong>${formatNumber(totalRequests)}</strong></div>
                <div><span>总 Tokens</span><strong>${formatNumber(totalTokens)}</strong></div>
                <div><span>总费用</span><strong>${formatMoney(totalCost)}</strong></div>
            </div>
            <svg viewBox="0 0 ${width} ${height}" role="img" aria-label="使用趋势图">
                <line x1="${paddingX}" y1="${paddingTop + chartHeight}" x2="${width - paddingX}" y2="${paddingTop + chartHeight}" stroke="rgba(148,163,184,0.45)"></line>
                ${bars}
                <polyline points="${linePoints}" fill="none" stroke="#0f172a" stroke-width="3" stroke-linecap="round" stroke-linejoin="round"></polyline>
                ${nodes}
                ${labels}
            </svg>
        `;
    }

    function registerPageCleanup(handler) {
        if (typeof handler === "function") {
            pageCleanupHandlers.push(handler);
        }
    }

    function runPageCleanup() {
        pageCleanupHandlers.forEach((handler) => {
            try {
                handler();
            } catch {
                // Cleanup must never block shell navigation.
            }
        });
        pageCleanupHandlers = [];
    }

    function formatMetricShort(value, digits = 1) {
        const normalized = Number(value ?? 0);
        if (!Number.isFinite(normalized)) return "-";
        if (Math.abs(normalized) >= 1000000) return `${(normalized / 1000000).toFixed(digits)}M`;
        if (Math.abs(normalized) >= 10000) return `${(normalized / 10000).toFixed(digits)}万`;
        if (Math.abs(normalized) >= 1000) return `${(normalized / 1000).toFixed(digits)}k`;
        return `${Math.round(normalized)}`;
    }

    function formatTimeLabel(value) {
        if (!value) return "-";
        const normalized = typeof value === "string" && !/[zZ]|[+\-]\d{2}:\d{2}$/.test(value)
            ? `${value.replace(" ", "T")}Z`
            : value;
        const date = new Date(normalized);
        if (Number.isNaN(date.getTime())) return "-";
        return date.toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit", hour12: false });
    }

    const MONITOR_TIME_WINDOWS = {
        180: { label: "最近 3 小时", bucketMinutes: 5, summaryMinutes: 180 },
        1440: { label: "最近 1 天", bucketMinutes: 15, summaryMinutes: 1440 },
        10080: { label: "最近 1 周", bucketMinutes: 120, summaryMinutes: 1440 },
        43200: { label: "最近 1 个月", bucketMinutes: 720, summaryMinutes: 1440 },
    };

    function getMonitorWindowConfig(value) {
        const windowMinutes = Number(value || 180);
        return MONITOR_TIME_WINDOWS[windowMinutes] || MONITOR_TIME_WINDOWS[180];
    }

    function formatMonitorBucketLabel(value, windowMinutes = 180) {
        if (!value) return "-";
        const normalized = typeof value === "string" && !/[zZ]|[+\-]\d{2}:\d{2}$/.test(value)
            ? `${value.replace(" ", "T")}Z`
            : value;
        const date = new Date(normalized);
        if (Number.isNaN(date.getTime())) return "-";
        if (Number(windowMinutes) > 1440) {
            return date.toLocaleDateString("zh-CN", { month: "2-digit", day: "2-digit" });
        }
        return date.toLocaleString("zh-CN", { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hour12: false });
    }

    function weightedAverage(rows, fieldName, weightName = "total_requests") {
        let weightedSum = 0;
        let weightSum = 0;
        rows.forEach((item) => {
            const value = Number(item[fieldName]);
            if (!Number.isFinite(value)) return;
            const weight = Math.max(1, Number(item[weightName] || 0));
            weightedSum += value * weight;
            weightSum += weight;
        });
        return weightSum ? Number((weightedSum / weightSum).toFixed(2)) : null;
    }

    function compactMonitorRows(items, maxPoints = 100) {
        const rows = Array.isArray(items) ? items : [];
        if (rows.length <= maxPoints) return rows;
        const bucketSize = Math.ceil(rows.length / maxPoints);
        const compacted = [];
        for (let index = 0; index < rows.length; index += bucketSize) {
            const group = rows.slice(index, index + bucketSize);
            compacted.push({
                bucket_start: group[0]?.bucket_start,
                total_requests: group.reduce((sum, item) => sum + Number(item.total_requests || 0), 0),
                success_requests: group.reduce((sum, item) => sum + Number(item.success_requests || 0), 0),
                failed_requests: group.reduce((sum, item) => sum + Number(item.failed_requests || 0), 0),
                stream_requests: group.reduce((sum, item) => sum + Number(item.stream_requests || 0), 0),
                image_requests: group.reduce((sum, item) => sum + Number(item.image_requests || 0), 0),
                total_tokens: group.reduce((sum, item) => sum + Number(item.total_tokens || 0), 0),
                total_cost: Number(group.reduce((sum, item) => sum + Number(item.total_cost || 0), 0).toFixed(9)),
                avg_latency_ms: weightedAverage(group, "avg_latency_ms"),
                avg_ttfb_ms: weightedAverage(group, "avg_ttfb_ms"),
                p95_latency_ms: Math.max(0, ...group.map((item) => Number(item.p95_latency_ms || 0))),
                p99_latency_ms: Math.max(0, ...group.map((item) => Number(item.p99_latency_ms || 0))),
                qps: weightedAverage(group, "qps") || 0,
                peak_active_requests: Math.max(0, ...group.map((item) => Number(item.peak_active_requests || 0))),
            });
        }
        return compacted;
    }

    function renderMonitorChart(container, items, options = {}) {
        if (!container) return;
        const rows = compactMonitorRows(items, options.maxPoints || 100);
        if (!rows.length) {
            container.innerHTML = '<div class="empty-state">暂无监控数据</div>';
            return;
        }
        const width = options.width || 720;
        const height = options.height || 260;
        const paddingX = 34;
        const paddingTop = 22;
        const paddingBottom = 36;
        const chartHeight = height - paddingTop - paddingBottom;
        const chartWidth = width - paddingX * 2;
        const barKey = options.barKey || "total_requests";
        const lineKey = options.lineKey || "failed_requests";
        const lineLabel = options.lineLabel || "";
        const barMax = Math.max(1, ...rows.map((item) => Number(item[barKey] || 0)));
        const lineMax = Math.max(1, ...rows.map((item) => Number(item[lineKey] || 0)));
        const step = rows.length > 1 ? chartWidth / (rows.length - 1) : chartWidth;
        const barWidth = Math.max(14, Math.min(42, chartWidth / Math.max(rows.length * 1.5, 1)));
        const bars = rows.map((item, index) => {
            const x = paddingX + index * step - barWidth / 2;
            const value = Number(item[barKey] || 0);
            const barHeight = Math.max(4, (value / barMax) * chartHeight);
            const y = paddingTop + chartHeight - barHeight;
            return `<rect x="${x.toFixed(2)}" y="${y.toFixed(2)}" width="${barWidth.toFixed(2)}" height="${barHeight.toFixed(2)}" rx="9" class="monitor-chart-bar"></rect>`;
        }).join("");
        const linePoints = rows.map((item, index) => {
            const x = paddingX + index * step;
            const y = paddingTop + chartHeight - (Number(item[lineKey] || 0) / lineMax) * chartHeight;
            return `${x.toFixed(2)},${y.toFixed(2)}`;
        }).join(" ");
        const nodes = rows.map((item, index) => {
            const x = paddingX + index * step;
            const y = paddingTop + chartHeight - (Number(item[lineKey] || 0) / lineMax) * chartHeight;
            return `<circle cx="${x.toFixed(2)}" cy="${y.toFixed(2)}" r="4" class="monitor-chart-node"><title>${escapeHtml(lineLabel)} ${escapeHtml(String(item[lineKey] ?? "-"))}</title></circle>`;
        }).join("");
        const labels = rows.map((item, index) => {
            if (index !== 0 && index !== rows.length - 1 && index % Math.ceil(rows.length / 4) !== 0) return "";
            const x = paddingX + index * step;
            return `<text x="${x.toFixed(2)}" y="${height - 12}" text-anchor="middle" class="monitor-chart-label">${escapeHtml(formatMonitorBucketLabel(item.bucket_start, options.windowMinutes))}</text>`;
        }).join("");
        container.innerHTML = `
            <svg viewBox="0 0 ${width} ${height}" role="img" aria-label="${escapeHtml(options.label || "监控趋势图")}">
                <line x1="${paddingX}" y1="${paddingTop + chartHeight}" x2="${width - paddingX}" y2="${paddingTop + chartHeight}" class="monitor-chart-axis"></line>
                <line x1="${paddingX}" y1="${paddingTop + chartHeight * 0.5}" x2="${width - paddingX}" y2="${paddingTop + chartHeight * 0.5}" class="monitor-chart-grid-line"></line>
                ${bars}
                <polyline points="${linePoints}" class="monitor-chart-line"></polyline>
                ${nodes}
                ${labels}
            </svg>
        `;
    }

    function renderMonitorRank(container, items, options = {}) {
        if (!container) return;
        const rows = Array.isArray(items) ? items.filter((item) => item && (item.requested_model || item.provider_name)) : [];
        if (!rows.length) {
            container.innerHTML = '<div class="empty-state">暂无模型数据</div>';
            return;
        }
        const maxValue = Math.max(1, ...rows.map((item) => Number(item.total_requests || 0)));
        container.innerHTML = rows.slice(0, options.limit || 6).map((item) => {
            const value = Number(item.total_requests || 0);
            const rate = Math.max(4, Math.round((value / maxValue) * 100));
            const title = item.requested_model || item.provider_name || "-";
            const meta = [
                `${formatNumber(value)} 次`,
                `失败率 ${formatPercent(item.failure_rate || 0)}`,
                item.avg_latency_ms != null ? `延迟 ${formatLatencyMs(item.avg_latency_ms)}` : null,
            ].filter(Boolean).join(" · ");
            return `
                <div class="monitor-rank-item">
                    <div class="monitor-rank-copy">
                        <strong>${escapeHtml(title)}</strong>
                        <span>${escapeHtml(meta)}</span>
                    </div>
                    <div class="monitor-rank-track" aria-hidden="true"><span style="width:${rate}%"></span></div>
                </div>
            `;
        }).join("");
    }

    function summarizeMetricItems(items) {
        const rows = Array.isArray(items) ? items : [];
        const totalRequests = rows.reduce((sum, item) => sum + Number(item.total_requests || 0), 0);
        const failedRequests = rows.reduce((sum, item) => sum + Number(item.failed_requests || 0), 0);
        const totalTokens = rows.reduce((sum, item) => sum + Number(item.total_tokens || 0), 0);
        const totalCost = rows.reduce((sum, item) => sum + Number(item.total_cost || 0), 0);
        const p95Latency = Math.max(0, ...rows.map((item) => Number(item.p95_latency_ms || 0)));
        const p95Ttfb = Math.max(0, ...rows.map((item) => Number(item.p95_ttfb_ms || 0)));
        return {
            totalRequests,
            failedRequests,
            totalTokens,
            totalCost,
            p95Latency,
            p95Ttfb,
            failureRate: totalRequests ? (failedRequests / totalRequests) * 100 : 0,
        };
    }

    function updateRefreshLabel(node, prefix = "每 30 秒刷新") {
        if (!node) return;
        node.textContent = `${prefix} · 最近刷新 ${new Date().toLocaleTimeString("zh-CN", { hour12: false })}`;
    }

    function formatStatusText(finishReason) {
        if (!finishReason) return "已返回结果";
        const mapping = {
            stop: "正常完成",
            length: "达到长度上限",
            tool_calls: "等待工具调用",
            content_filter: "被内容过滤中断",
        };
        return `${mapping[finishReason] || "已结束"} (${finishReason})`;
    }

    function extractUsageDetails(usage) {
        if (!usage || typeof usage !== "object") {
            return null;
        }
        return {
            promptTokens: usage.prompt_tokens ?? usage.input_tokens ?? null,
            completionTokens: usage.completion_tokens ?? usage.output_tokens ?? null,
            totalTokens: usage.total_tokens ?? null,
            cachedTokens: usage.prompt_tokens_details?.cached_tokens ?? usage.input_tokens_details?.cached_tokens ?? null,
            reasoningTokens: usage.completion_tokens_details?.reasoning_tokens ?? usage.output_tokens_details?.reasoning_tokens ?? null,
        };
    }

    function extractTextValue(value) {
        if (typeof value === "string") {
            return value;
        }
        if (Array.isArray(value)) {
            return value.map((item) => extractTextValue(item)).filter(Boolean).join("");
        }
        if (!value || typeof value !== "object") {
            return "";
        }
        if (typeof value.text === "string") {
            return value.text;
        }
        if (value.text && typeof value.text === "object") {
            if (typeof value.text.value === "string") {
                return value.text.value;
            }
            return extractTextValue(value.text);
        }
        if (typeof value.value === "string") {
            return value.value;
        }
        if (typeof value.content === "string") {
            return value.content;
        }
        if (Array.isArray(value.content)) {
            return value.content.map((item) => extractTextValue(item)).filter(Boolean).join("");
        }
        return "";
    }

    function collectTextFragments(value, parts = []) {
        if (value == null) return parts;
        if (typeof value === "string") {
            if (value.trim()) parts.push(value);
            return parts;
        }
        if (Array.isArray(value)) {
            value.forEach((item) => collectTextFragments(item, parts));
            return parts;
        }
        if (typeof value !== "object") {
            return parts;
        }
        const directText = extractTextValue(value);
        if (directText && directText.trim()) {
            parts.push(directText);
            return parts;
        }
        Object.values(value).forEach((item) => collectTextFragments(item, parts));
        return parts;
    }

    function extractAssistantText(data) {
        if (!data || typeof data !== "object") return "";
        if (Array.isArray(data.choices) && data.choices.length) {
            const choice = data.choices[0];
            const messageContent = extractTextValue(choice?.message?.content);
            if (messageContent.trim()) {
                return messageContent.trim();
            }
            const deltaContent = extractTextValue(choice?.delta?.content);
            if (deltaContent.trim()) {
                return deltaContent.trim();
            }
        }
        if (typeof data.output_text === "string") {
            return data.output_text;
        }
        if (typeof data.delta === "string" && data.type === "response.output_text.delta") {
            return data.delta;
        }
        if (Array.isArray(data.output)) {
            const parts = collectTextFragments(data.output, []);
            return parts.join("\n").trim();
        }
        if (Array.isArray(data.content)) {
            const parts = collectTextFragments(data.content, []);
            return parts.join("\n").trim();
        }
        if (data.response && typeof data.response === "object") {
            return extractAssistantText(data.response);
        }
        return "";
    }

    function extractReasoningText(data) {
        if (!data || typeof data !== "object") return "";
        if (Array.isArray(data.choices) && data.choices.length) {
            const reasoning = data.choices[0]?.message?.reasoning_content;
            if (typeof reasoning === "string" && reasoning.trim()) {
                return reasoning.trim();
            }
        }
        return "";
    }

    function parseSsePayload(text) {
        const events = [];
        let usage = null;
        let finishReason = null;
        const replyParts = [];
        let latestResponse = null;
        for (const line of text.split(/\r?\n/)) {
            const trimmed = line.trim();
            if (!trimmed.startsWith("data:")) continue;
            const payload = trimmed.slice(5).trim();
            if (!payload || payload === "[DONE]") continue;
            const parsed = safeJsonParse(payload);
            if (!parsed || typeof parsed !== "object") continue;
            events.push(parsed);
            if (parsed.response && typeof parsed.response === "object") {
                latestResponse = parsed.response;
            }
            if (parsed.type === "response.output_text.delta" && typeof parsed.delta === "string") {
                replyParts.push(parsed.delta);
            }
            const deltaText = extractAssistantText(parsed);
            if (deltaText) replyParts.push(deltaText);
            if (!finishReason) {
                finishReason = parsed.choices?.[0]?.finish_reason
                    || parsed.output?.[0]?.finish_reason
                    || parsed.response?.output?.[0]?.finish_reason
                    || parsed.response?.status
                    || null;
            }
            if (!usage && parsed.usage) {
                usage = parsed.usage;
            } else if (parsed.usage) {
                usage = parsed.usage;
            }
            if (parsed.response?.usage) {
                usage = parsed.response.usage;
            }
        }
        const first = events[0] || {};
        return {
            id: latestResponse?.id || first.id || null,
            model: latestResponse?.model || first.model || null,
            created: latestResponse?.created_at || latestResponse?.created || first.created || null,
            usage,
            finishReason,
            reply: replyParts.join(""),
        };
    }

    function formatTimestamp(value) {
        if (!value) return "-";
        const date = typeof value === "number" ? new Date(value * 1000) : new Date(value);
        if (Number.isNaN(date.getTime())) return String(value);
        return date.toLocaleString("zh-CN");
    }

    function renderInlineRichText(text) {
        return escapeHtml(text)
            .replace(/`([^`]+)`/g, "<code>$1</code>")
            .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
    }

    function renderTextParagraphs(text) {
        const blocks = text
            .split(/\n{2,}/)
            .map((item) => item.trim())
            .filter(Boolean);
        if (!blocks.length) {
            return `<p>${renderInlineRichText(text)}</p>`;
        }
        return blocks.map((block) => {
            const lines = block.split("\n").map((line) => line.trim()).filter(Boolean);
            if (lines.length && lines.every((line) => /^\d+\.\s+/.test(line))) {
                return `<ol>${lines.map((line) => `<li>${renderInlineRichText(line.replace(/^\d+\.\s+/, ""))}</li>`).join("")}</ol>`;
            }
            if (lines.length && lines.every((line) => /^[-*]\s+/.test(line))) {
                return `<ul>${lines.map((line) => `<li>${renderInlineRichText(line.replace(/^[-*]\s+/, ""))}</li>`).join("")}</ul>`;
            }
            return `<p>${renderInlineRichText(block).replace(/\n/g, "<br>")}</p>`;
        }).join("");
    }

    function renderReplyContent(text) {
        const source = String(text || "").trim();
        if (!source) {
            return "<p>未提取到可显示的回复内容</p>";
        }
        const segments = [];
        let lastIndex = 0;
        const codeBlockPattern = /```([\w-]+)?\n?([\s\S]*?)```/g;
        let match;
        while ((match = codeBlockPattern.exec(source)) !== null) {
            const plainText = source.slice(lastIndex, match.index).trim();
            if (plainText) {
                segments.push(`<div class="reply-rich-text">${renderTextParagraphs(plainText)}</div>`);
            }
            const language = match[1] ? `<span class="reply-code-lang">${escapeHtml(match[1])}</span>` : "";
            segments.push(`
                <div class="reply-code-block">
                    ${language}
                    <pre><code>${escapeHtml(match[2].trim())}</code></pre>
                </div>
            `);
            lastIndex = match.index + match[0].length;
        }
        const trailing = source.slice(lastIndex).trim();
        if (trailing) {
            segments.push(`<div class="reply-rich-text">${renderTextParagraphs(trailing)}</div>`);
        }
        return segments.join("") || `<div class="reply-rich-text">${renderTextParagraphs(source)}</div>`;
    }

    function renderPlaygroundStatusMeta(context) {
        return `
            <div class="playground-status-bar">
                <span>接口: ${escapeHtml(context.endpointLabel || "chat/completions")}</span>
                <span>命中中转: ${escapeHtml(context.providerName || "-")}</span>
                <span>耗时: ${escapeHtml(context.latencyMs || "-")} ms</span>
                <span>模式: ${escapeHtml(context.isStream ? "stream" : "json")}</span>
            </div>
        `;
    }

    function renderStreamingPlaygroundResponse(replyText, context = {}, statusText = "流式输出中...") {
        return `
            ${renderPlaygroundStatusMeta(context)}
            <section class="playground-result-card">
                <div class="playground-card-title">${escapeHtml(statusText)}</div>
                <div class="playground-reply-content">${renderReplyContent(replyText || "等待首个分片...")}</div>
            </section>
        `;
    }

    function readProxyProviderName(headers) {
        const rawValue = headers.get("X-Proxy-Provider-Name");
        if (!rawValue) {
            return "-";
        }
        const encoding = headers.get("X-Proxy-Provider-Name-Encoding");
        if (encoding !== "utf-8-percent-encoded") {
            return rawValue;
        }
        try {
            return decodeURIComponent(rawValue);
        } catch (error) {
            console.warn("Failed to decode X-Proxy-Provider-Name header", error);
            return rawValue;
        }
    }

    async function readStreamingResponse(response, payload, meta) {
        if (!response.body) {
            throw new Error("当前浏览器不支持流式读取响应");
        }

        const context = {
            isStream: true,
            model: payload.model,
            endpointLabel: payload.endpointLabel,
            providerName: readProxyProviderName(response.headers),
            latencyMs: response.headers.get("X-Proxy-Latency-Ms") || "-",
        };
        const reader = response.body.getReader();
        const decoder = new TextDecoder("utf-8");
        let aggregatedText = "";
        let renderedReply = "";

        showPlaygroundRendered(renderStreamingPlaygroundResponse("", context));
        meta.innerHTML = "";

        while (true) {
            const { value, done } = await reader.read();
            if (done) break;
            aggregatedText += decoder.decode(value, { stream: true });
            const parsed = parseSsePayload(aggregatedText);
            const nextReply = parsed.reply || "";
            if (nextReply !== renderedReply) {
                renderedReply = nextReply;
                showPlaygroundRendered(renderStreamingPlaygroundResponse(renderedReply, context));
            }
        }

        aggregatedText += decoder.decode();
        const finalParsed = parseSsePayload(aggregatedText);
        showPlaygroundRendered(renderPlaygroundResponse(
            {
                id: finalParsed.id,
                model: finalParsed.model || payload.model,
                created: finalParsed.created,
                usage: finalParsed.usage,
                choices: [{
                    message: { content: finalParsed.reply || renderedReply },
                    finish_reason: finalParsed.finishReason,
                }],
            },
            context,
        ));
    }

    function renderPlaygroundResponse(data, context = {}) {
        if (typeof data === "string") {
            const sseParsed = context.isStream ? parseSsePayload(data) : null;
            if (sseParsed && (sseParsed.reply || sseParsed.usage || sseParsed.id)) {
                return renderPlaygroundResponse(
                    {
                        id: sseParsed.id,
                        model: sseParsed.model,
                        created: sseParsed.created,
                        usage: sseParsed.usage,
                        choices: [{
                            message: { content: sseParsed.reply },
                            finish_reason: sseParsed.finishReason,
                        }],
                    },
                    context,
                );
            }
            return `
                ${renderPlaygroundStatusMeta(context)}
                <section class="playground-result-card">
                    <div class="playground-card-title">直接返回内容</div>
                    <div class="playground-reply-content">${renderReplyContent(data || "无返回内容")}</div>
                </section>
            `;
        }

        const replyText = extractAssistantText(data) || "未提取到可显示的回复内容";
        const reasoningText = extractReasoningText(data);
        const usage = extractUsageDetails(data.usage);
        const finishReason = data?.choices?.[0]?.finish_reason || data?.output?.[0]?.finish_reason || null;
        const statusText = formatStatusText(finishReason);
        const modelName = data?.model || context.model || "-";
        const responseId = data?.id || "-";
        const createdAt = formatTimestamp(data?.created);

        const tokenCard = usage ? `
            <section class="playground-result-card">
                <div class="playground-card-title">Token 统计</div>
                <div class="playground-token-grid">
                    <div class="playground-token-item"><div class="playground-token-label">提示词</div><div class="playground-token-value">${usage.promptTokens ?? "-"}</div></div>
                    <div class="playground-token-item"><div class="playground-token-label">生成</div><div class="playground-token-value">${usage.completionTokens ?? "-"}</div></div>
                    <div class="playground-token-item"><div class="playground-token-label">总计</div><div class="playground-token-value">${usage.totalTokens ?? "-"}</div></div>
                    <div class="playground-token-item"><div class="playground-token-label">缓存 / 推理</div><div class="playground-token-value">${usage.cachedTokens ?? 0} / ${usage.reasoningTokens ?? 0}</div></div>
                </div>
            </section>
        ` : "";

        const reasoningCard = reasoningText ? `
            <section class="playground-result-card">
                <div class="playground-card-title">推理摘要</div>
                <div class="playground-reply-content">${renderReplyContent(reasoningText)}</div>
            </section>
        ` : "";

        return `
            ${renderPlaygroundStatusMeta(context)}
            <section class="playground-result-card">
                <div class="playground-card-title">内部代理响应卡片</div>
                <div class="playground-info-row">
                    <div class="playground-info-label">模型</div>
                    <div class="playground-info-value">${escapeHtml(modelName)}</div>
                </div>
                <div class="playground-info-row">
                    <div class="playground-info-label">状态</div>
                    <div class="playground-info-value playground-status-success">${escapeHtml(statusText)}</div>
                </div>
                <div class="playground-info-row">
                    <div class="playground-info-label">响应 ID</div>
                    <div class="playground-info-value">
                        <span title="${escapeHtml(responseId)}">${escapeHtml(responseId)}</span>
                        ${responseId !== "-" ? `<button class="playground-copy-btn" type="button" data-copy-text="${escapeHtml(responseId)}">复制</button>` : ""}
                    </div>
                </div>
                <div class="playground-info-row">
                    <div class="playground-info-label">创建时间</div>
                    <div class="playground-info-value">${escapeHtml(createdAt)}</div>
                </div>
            </section>
            <section class="playground-result-card">
                <div class="playground-card-title">模型回复</div>
                <div class="playground-reply-content">${renderReplyContent(replyText)}</div>
            </section>
            ${tokenCard}
            ${reasoningCard}
        `;
    }

    function animateButtonPress(button) {
        if (!button) return;
        button.classList.remove("is-pressed");
        void button.offsetWidth;
        button.classList.add("is-pressed");
        setTimeout(() => button.classList.remove("is-pressed"), 280);
    }

    function triggerRipple(button, event) {
        if (!button || !event) return;
        const rect = button.getBoundingClientRect();
        const x = event.clientX - rect.left;
        const y = event.clientY - rect.top;
        button.style.setProperty("--ripple-x", `${x}px`);
        button.style.setProperty("--ripple-y", `${y}px`);
        button.classList.remove("ripple-active");
        void button.offsetWidth;
        button.classList.add("ripple-active");
        setTimeout(() => button.classList.remove("ripple-active"), 560);
    }

    function pulseButton(button) {
        if (!button) return;
        button.classList.remove("glow-pulse");
        void button.offsetWidth;
        button.classList.add("glow-pulse");
        setTimeout(() => button.classList.remove("glow-pulse"), 560);
    }

    function setButtonLoading(button, isLoading) {
        if (!button) return;
        button.classList.toggle("is-loading", isLoading);
        button.setAttribute("aria-busy", isLoading ? "true" : "false");
        if (isLoading) {
            if (button.dataset.loadingDisabledSnapshot == null) {
                button.dataset.loadingDisabledSnapshot = button.disabled ? "true" : "false";
            }
            button.disabled = true;
            return;
        }
        if (button.dataset.loadingDisabledSnapshot != null) {
            button.disabled = button.dataset.loadingDisabledSnapshot === "true";
            delete button.dataset.loadingDisabledSnapshot;
        } else {
            button.disabled = false;
        }
    }

    function enhanceInteractiveButtons(scope = document) {
        scope.querySelectorAll(".interactive-btn, .table-action-btn, .playground-copy-btn").forEach((button) => {
            if (button.dataset.animated === "true") return;
            button.dataset.animated = "true";
            button.addEventListener("pointerdown", (event) => {
                animateButtonPress(button);
                triggerRipple(button, event);
            });
            button.addEventListener("click", () => pulseButton(button));
        });
    }

    async function initDashboard() {
        const checkAllBtn = document.getElementById("check-all-btn");
        if (checkAllBtn) {
            checkAllBtn.addEventListener("click", async () => {
                try {
                    setButtonLoading(checkAllBtn, true);
                    await runHealthCheckStream({
                        url: "/api/providers/test-all-stream",
                        title: "全部中转站健康检查",
                        scope: "all",
                        trigger: checkAllBtn,
                        showModal: true,
                    });
                    setButtonLoading(checkAllBtn, false);
                    setButtonTransientFeedback(checkAllBtn, "success", { successText: "已完成" });
                    showToast("已完成全部中转健康检查");
                    await refreshDashboard();
                } catch (error) {
                    setButtonLoading(checkAllBtn, false);
                    setButtonTransientFeedback(checkAllBtn, "error", { errorText: "失败" });
                    showToast(error.message, "error");
                } finally {
                    setButtonLoading(checkAllBtn, false);
                }
            });
        }
        await refreshDashboard();
        initDashboardLiveMonitor();
    }

    function initDashboardLiveMonitor() {
        const refreshBtn = document.getElementById("dashboard-monitor-refresh-btn");
        const trafficWindowSelect = document.getElementById("dashboard-monitor-traffic-window");
        const latencyWindowSelect = document.getElementById("dashboard-monitor-latency-window");
        if (!document.getElementById("dashboard-live-monitor")) return;
        let loading = false;
        const load = async (manual = false) => {
            if (loading) return;
            loading = true;
            try {
                await refreshDashboardMonitor();
                if (manual) showToast("监控数据已刷新");
            } catch (error) {
                if (manual) showToast(error.message, "error");
            } finally {
                loading = false;
            }
        };
        refreshBtn?.addEventListener("click", () => load(true));
        trafficWindowSelect?.addEventListener("change", () => load(true));
        latencyWindowSelect?.addEventListener("change", () => load(true));
        load(false);
        const timer = window.setInterval(() => load(false), 30000);
        registerPageCleanup(() => window.clearInterval(timer));
    }

    async function refreshDashboardMonitor() {
        const trafficWindowSelect = document.getElementById("dashboard-monitor-traffic-window");
        const latencyWindowSelect = document.getElementById("dashboard-monitor-latency-window");
        const trafficWindow = getMonitorWindowConfig(trafficWindowSelect?.value);
        const latencyWindow = getMonitorWindowConfig(latencyWindowSelect?.value);
        const trafficWindowMinutes = Number(trafficWindowSelect?.value || 180);
        const latencyWindowMinutes = Number(latencyWindowSelect?.value || 180);
        const [summary, trafficSeries, latencySeries] = await Promise.all([
            api.get(`/api/metrics/summary?window_minutes=${encodeURIComponent(trafficWindow.summaryMinutes)}`),
            api.get(`/api/metrics/timeseries?window_minutes=${encodeURIComponent(trafficWindowMinutes)}&bucket_minutes=${encodeURIComponent(trafficWindow.bucketMinutes)}`),
            api.get(`/api/metrics/timeseries?window_minutes=${encodeURIComponent(latencyWindowMinutes)}&bucket_minutes=${encodeURIComponent(latencyWindow.bucketMinutes)}`),
        ]);
        const metricItems = Array.isArray(summary.items) ? summary.items : [];
        const trafficItems = Array.isArray(trafficSeries.items) ? trafficSeries.items : [];
        const latencyItems = Array.isArray(latencySeries.items) ? latencySeries.items : [];
        const trafficOverview = summarizeMetricItems(trafficItems);
        const latencyOverview = summarizeMetricItems(latencyItems);
        const requestTotal = document.getElementById("dashboard-monitor-request-total");
        if (requestTotal) requestTotal.textContent = `${formatMetricShort(trafficOverview.totalRequests)} 次`;
        const latencyValue = document.getElementById("dashboard-monitor-latency-value");
        if (latencyValue) latencyValue.textContent = `${formatLatencyMs(latencyOverview.p95Latency)} / ${formatLatencyMs(latencyOverview.p95Ttfb)}`;
        const modelCount = document.getElementById("dashboard-monitor-model-count");
        if (modelCount) modelCount.textContent = `${new Set(metricItems.map((item) => item.requested_model).filter(Boolean)).size} 个`;
        const providerValue = document.getElementById("dashboard-monitor-provider-value");
        if (providerValue) providerValue.textContent = `失败率 ${formatPercent(trafficOverview.failureRate)}`;
        renderMonitorChart(document.getElementById("dashboard-monitor-traffic-chart"), trafficItems, {
            barKey: "total_requests",
            lineKey: "failed_requests",
            lineLabel: "失败",
            label: "请求量与失败趋势",
            windowMinutes: trafficWindowMinutes,
        });
        renderMonitorChart(document.getElementById("dashboard-monitor-latency-chart"), latencyItems, {
            barKey: "avg_ttfb_ms",
            lineKey: "p95_latency_ms",
            lineLabel: "P95 延迟",
            label: "延迟与首包趋势",
            width: 520,
            height: 220,
            windowMinutes: latencyWindowMinutes,
        });
        renderMonitorRank(document.getElementById("dashboard-monitor-model-rank"), metricItems);
        renderDashboardProviderStrip(metricItems);
        updateRefreshLabel(document.getElementById("dashboard-monitor-refresh-label"));
    }

    function renderDashboardProviderStrip(metricItems) {
        const container = document.getElementById("dashboard-monitor-provider-strip");
        if (!container) return;
        const grouped = new Map();
        (Array.isArray(metricItems) ? metricItems : []).forEach((item) => {
            const key = item.provider_name || "未命名中转";
            const current = grouped.get(key) || {
                provider_name: key,
                total_requests: 0,
                failed_requests: 0,
                avg_latency_sum: 0,
                latency_samples: 0,
            };
            current.total_requests += Number(item.total_requests || 0);
            current.failed_requests += Number(item.failed_requests || 0);
            if (item.avg_latency_ms != null) {
                current.avg_latency_sum += Number(item.avg_latency_ms);
                current.latency_samples += 1;
            }
            grouped.set(key, current);
        });
        const rows = Array.from(grouped.values()).sort((a, b) => b.total_requests - a.total_requests).slice(0, 8);
        if (!rows.length) {
            container.innerHTML = '<div class="empty-state">暂无中转流量数据</div>';
            return;
        }
        container.innerHTML = rows.map((item) => {
            const failureRate = item.total_requests ? (item.failed_requests / item.total_requests) * 100 : 0;
            const avgLatency = item.latency_samples ? item.avg_latency_sum / item.latency_samples : null;
            const tone = failureRate >= 20 ? "danger" : failureRate >= 5 ? "warn" : "ok";
            return `
                <div class="monitor-provider-pill" data-tone="${tone}">
                    <strong>${escapeHtml(item.provider_name)}</strong>
                    <span>${formatNumber(item.total_requests)} 次 · 失败 ${formatPercent(failureRate)} · ${formatLatencyMs(avgLatency)}</span>
                </div>
            `;
        }).join("");
    }

    function renderDashboardUsageRows(items, options = {}) {
        const rows = Array.isArray(items) ? items : [];
        const showProvider = options.showProvider === true;
        const showModel = options.showModel === true;
        const colspan = Number(options.colspan || 4);
        if (!rows.length) {
            return `<tr><td colspan="${colspan}"><div class="empty-state">${escapeHtml(options.emptyText || "暂无累计统计数据")}</div></td></tr>`;
        }
        return rows.map((item) => `
            <tr>
                ${showProvider ? `<td>${escapeHtml(item.provider_name || "-")}</td>` : ""}
                ${showModel ? `<td>${escapeHtml(item.model_name || "-")}</td>` : ""}
                <td>${formatNumber(item.total_requests || 0)}</td>
                <td>${formatNumber(item.total_tokens || 0)}</td>
                <td>${formatMoney(item.total_cost || 0)}</td>
            </tr>
        `).join("");
    }

    function renderDashboardUsageOverview(usageOverview = {}) {
        const summary = usageOverview.summary || {};
        const summaryNode = document.getElementById("dashboard-usage-summary");
        if (summaryNode) {
            summaryNode.innerHTML = `
                <div><span>总请求数</span><strong>${formatNumber(summary.total_requests || 0)}</strong></div>
                <div><span>总 Token</span><strong>${formatNumber(summary.total_tokens || 0)}</strong></div>
                <div><span>输入 / 输出 Token</span><strong>${formatNumber(summary.prompt_tokens || 0)} / ${formatNumber(summary.completion_tokens || 0)}</strong></div>
                <div><span>总价格成本</span><strong>${formatMoney(summary.total_cost || 0)}</strong></div>
            `;
        }
        const modelBody = document.getElementById("dashboard-usage-model-body");
        if (modelBody) {
            modelBody.innerHTML = renderDashboardUsageRows(usageOverview.top_models, {
                showModel: true,
                colspan: 4,
                emptyText: "暂无模型成本数据",
            });
        }
        const providerBody = document.getElementById("dashboard-usage-provider-body");
        if (providerBody) {
            providerBody.innerHTML = renderDashboardUsageRows(usageOverview.top_providers, {
                showProvider: true,
                colspan: 4,
                emptyText: "暂无中转站成本数据",
            });
        }
        const providerModelBody = document.getElementById("dashboard-usage-provider-model-body");
        if (providerModelBody) {
            providerModelBody.innerHTML = renderDashboardUsageRows(usageOverview.top_provider_models, {
                showProvider: true,
                showModel: true,
                colspan: 5,
                emptyText: "暂无中转站模型成本数据",
            });
        }
        const cacheNote = document.getElementById("dashboard-usage-cache-note");
        if (cacheNote) {
            cacheNote.textContent = `精确聚合，${formatNumber(usageOverview.cache_ttl_seconds || 30)} 秒短缓存`;
        }
    }

    async function refreshDashboard() {
        const [stats, providers, settings, metrics, timeSeries] = await Promise.all([
            api.get("/api/dashboard"),
            api.get("/api/providers"),
            api.get("/api/settings"),
            api.get("/api/metrics/summary?window_minutes=60"),
            api.get("/api/metrics/timeseries?window_minutes=180&bucket_minutes=15"),
        ]);
        document.querySelector('[data-stat="provider_count"]').textContent = stats.provider_count;
        document.querySelector('[data-stat="healthy_count"]').textContent = stats.healthy_count;
        document.querySelector('[data-stat="degraded_count"]').textContent = stats.degraded_count;
        document.querySelector('[data-stat="unhealthy_count"]').textContent = stats.unhealthy_count;
        document.querySelector('[data-stat="model_count"]').textContent = stats.model_count;
        document.querySelector('[data-stat="recent_requests"]').textContent = stats.recent_requests;
        document.querySelector('[data-stat="recent_tokens"]').textContent = stats.recent_tokens;
        document.querySelector('[data-stat="total_requests"]').textContent = formatNumber(stats.total_requests || 0);
        document.querySelector('[data-stat="total_tokens"]').textContent = formatNumber(stats.total_tokens || 0);
        document.querySelector('[data-stat="total_cost"]').textContent = formatMoney(stats.total_cost || 0);
        document.querySelector('[data-stat="conversation_count"]').textContent = stats.conversation_count;
        document.querySelector('[data-stat="api_key_total"]').textContent = stats.api_key_total;
        document.querySelector('[data-stat="api_key_enabled"]').textContent = stats.api_key_enabled;
        document.querySelector('[data-stat="api_key_disabled"]').textContent = stats.api_key_disabled;
        document.querySelector('[data-stat="api_key_total_requests"]').textContent = stats.api_key_total_requests;
        document.querySelector('[data-stat="api_key_total_prompt_tokens"]').textContent = stats.api_key_total_prompt_tokens;
        document.querySelector('[data-stat="api_key_total_completion_tokens"]').textContent = stats.api_key_total_completion_tokens;
        document.querySelector('[data-stat="api_key_total_tokens"]').textContent = stats.api_key_total_tokens;
        document.querySelector('[data-stat="recent_failure_rate"]').textContent = `${stats.recent_failure_rate}%`;
        document.querySelector('[data-stat="total_failures"]').textContent = stats.total_failures;
        renderDashboardUsageOverview(stats.usage_overview || {});

        const grid = document.getElementById("dashboard-provider-grid");
        grid.innerHTML = providers.map((provider) => `
            <article class="provider-card">
                <div class="provider-card-top">
                    <h4>${escapeHtml(provider.name)}</h4>
                    ${statusBadge(provider.health_status)}
                </div>
                <div class="provider-meta">优先级 ${provider.priority} / 权重 ${provider.weight}</div>
                <div class="provider-models">${escapeHtml(provider.models.join(", ") || "-")}</div>
                <div class="provider-foot">
                    <span>延迟 ${provider.last_latency_ms ?? "-"} ms</span>
                    <span>${escapeHtml(formatCircuitStateLabel(provider.circuit_state))}</span>
                </div>
            </article>
        `).join("") || '<div class="empty-state">暂无中转站数据</div>';

        const defaultProvider = providers.find((item) => item.id === settings.default_provider_id);
        document.getElementById("dashboard-route-meta").innerHTML = `
            <div><span>模式</span><strong>${escapeHtml(formatRouteModeLabel(settings.route_mode))}</strong></div>
            <div><span>默认中转</span><strong>${defaultProvider ? escapeHtml(defaultProvider.name) : "-"}</strong></div>
            <div><span>自动巡检</span><strong>${formatSwitchText(settings.auto_health_check)}</strong></div>
            <div><span>检查间隔</span><strong>${settings.health_check_interval_sec} 秒</strong></div>
            <div><span>模型健康</span><strong>${formatHealthOverview(stats.healthy_model_count, stats.degraded_model_count, stats.unhealthy_model_count)}</strong></div>
            <div><span>Token 记录</span><strong>${formatSwitchText(settings.enable_token_logging)}</strong></div>
            <div><span>正文记录</span><strong>${formatSwitchText(settings.enable_payload_logging)}</strong></div>
        `;

        const healthRatio = stats.provider_count ? Math.round((stats.healthy_count / stats.provider_count) * 100) : 0;
        document.getElementById("dashboard-signal-card").innerHTML = `
            <div class="cockpit-aside-label">系统信号</div>
            <div class="cockpit-aside-value">${stats.recent_requests}</div>
            <div class="cockpit-aside-copy">过去 24 小时代理请求量</div>
            <div class="cockpit-health-bar"><span style="width:${healthRatio}%"></span></div>
            <div class="cockpit-aside-meta">
                <span>路由 ${escapeHtml(formatRouteModeLabel(settings.route_mode))}</span>
                <span>${stats.healthy_count}/${stats.provider_count} 健康</span>
            </div>
        `;

        document.getElementById("dashboard-health-distribution").innerHTML = `
            <div><span>健康</span><strong>${stats.healthy_count}</strong></div>
            <div><span>降级</span><strong>${stats.degraded_count}</strong></div>
            <div><span>异常</span><strong>${stats.unhealthy_count}</strong></div>
            <div><span>模型数</span><strong>${stats.model_count}</strong></div>
        `;

        document.getElementById("dashboard-logging-profile").innerHTML = `
            <div><span>Token 统计</span><strong>${formatSwitchText(settings.enable_token_logging)}</strong></div>
            <div><span>正文保存</span><strong>${formatSwitchText(settings.enable_payload_logging)}</strong></div>
            <div><span>流式留存</span><strong>${formatSwitchText(settings.enable_stream_response_persist)}</strong></div>
            <div><span>日志大小上限</span><strong>${settings.max_logged_body_bytes} B</strong></div>
        `;

        const metricsBody = document.getElementById("dashboard-metrics-table-body");
        const metricItems = Array.isArray(metrics.items) ? metrics.items : [];
        metricsBody.innerHTML = metricItems.length ? metricItems.slice(0, 12).map((item) => `
            <tr>
                <td>${escapeHtml(item.provider_name || "-")}</td>
                <td>${escapeHtml(item.requested_model || "-")}</td>
                <td>${formatNumber(item.total_requests)}</td>
                <td>${formatNumber(item.success_requests)}</td>
                <td>${item.failure_rate}%</td>
                <td>${item.avg_latency_ms ?? "-"} ms</td>
                <td>${item.avg_ttfb_ms ?? "-"} ms</td>
                <td>${formatNumber(item.stream_requests || 0)}</td>
                <td>${formatNumber(item.image_requests || 0)}</td>
                <td>${formatNumber(item.unique_users || 0)}</td>
            </tr>
        `).join("") : '<tr><td colspan="10" class="text-muted">最近 60 分钟暂无可展示流量</td></tr>';

        const timeSeriesBody = document.getElementById("dashboard-timeseries-table-body");
        const timeSeriesItems = Array.isArray(timeSeries.items) ? timeSeries.items : [];
        timeSeriesBody.innerHTML = timeSeriesItems.length ? timeSeriesItems.slice(-12).reverse().map((item) => `
            <tr>
                <td>${formatDate(item.bucket_start)}</td>
                <td>${formatNumber(item.total_requests)}</td>
                <td>${formatNumber(item.success_requests)}</td>
                <td>${formatNumber(item.failed_requests)}</td>
                <td>${formatNumber(item.stream_requests || 0)}</td>
                <td>${formatNumber(item.image_requests || 0)}</td>
                <td>${item.avg_latency_ms ?? "-"} ms</td>
                <td>${item.avg_ttfb_ms ?? "-"} ms</td>
                <td>${formatNumber(item.total_tokens || 0)}</td>
            </tr>
        `).join("") : '<tr><td colspan="9" class="text-muted">最近 3 小时暂无时间序列数据</td></tr>';
    }

    async function initProviders() {
        const tableBody = document.getElementById("provider-table-body");
        const modelTableBody = document.getElementById("provider-model-table-body");
        const modal = document.getElementById("provider-modal");
        const modelsDetailModal = document.getElementById("provider-models-detail-modal");
        const modelsDetailModalTitle = document.getElementById("provider-models-detail-modal-title");
        const modelsDetailModalContent = document.getElementById("provider-models-detail-modal-content");
        const credentialModal = document.getElementById("provider-credential-modal");
        const credentialForm = document.getElementById("provider-credential-form");
        const credentialProviderIdInput = document.getElementById("provider-credential-provider-id");
        const credentialApiKeyInput = document.getElementById("provider-credential-api-key");
        const credentialHintInput = document.getElementById("provider-credential-hint");
        const credentialSubmitBtn = document.getElementById("provider-credential-submit-btn");
        const availabilityProviderSelect = document.getElementById("provider-availability-provider");
        const availabilityWindowSelect = document.getElementById("provider-availability-window");
        const availabilityBucketSelect = document.getElementById("provider-availability-bucket");
        const availabilityRefreshBtn = document.getElementById("provider-availability-refresh-btn");
        const availabilityTableBody = document.getElementById("provider-availability-table-body");
        const searchInput = document.getElementById("provider-search");
        const providerModelSearchInput = document.getElementById("provider-model-search");
        const providerModelProviderSelect = document.getElementById("provider-model-provider-id");
        const providerModelEnabledSelect = document.getElementById("provider-model-enabled");
        const providerModelHealthSelect = document.getElementById("provider-model-health");
        const providerModelPageSizeSelect = document.getElementById("provider-model-page-size");
        const providerModelPageMeta = document.getElementById("provider-model-page-meta");
        const providerModelPrevPageBtn = document.getElementById("provider-model-prev-page-btn");
        const providerModelNextPageBtn = document.getElementById("provider-model-next-page-btn");
        const checkAllBtn = document.getElementById("providers-check-all-btn");
        const submitBtn = document.getElementById("provider-submit-btn");
        const providerModelConfigList = document.getElementById("provider-model-config-list");
        const customModelInput = document.getElementById("provider-custom-model-name");
        const addCustomModelBtn = document.getElementById("provider-add-custom-model");
        const addEmptyModelBtn = document.getElementById("provider-add-empty-model");
        const presetManageBtn = document.getElementById("provider-preset-manage-btn");
        const presetList = document.getElementById("provider-model-preset-list");
        const presetManager = document.getElementById("provider-preset-manager");
        const presetEditorList = document.getElementById("provider-preset-editor-list");
        const newPresetInput = document.getElementById("provider-new-preset-model");
        const addPresetBtn = document.getElementById("provider-add-preset-model");
        const providerForm = document.getElementById("provider-form");
        const providerIdInput = document.getElementById("provider-id");
        const providerNameInput = document.getElementById("provider-name");
        const providerBaseUrlInput = document.getElementById("provider-base-url");
        const providerApiKeyInput = document.getElementById("provider-api-key");
        const providerTypeInput = document.getElementById("provider-type");
        const providerGroupNameInput = document.getElementById("provider-group-name");
        const providerRegionTagInput = document.getElementById("provider-region-tag");
        const providerPriorityInput = document.getElementById("provider-priority");
        const providerWeightInput = document.getElementById("provider-weight");
        const providerTimeoutMsInput = document.getElementById("provider-timeout-ms");
        const providerMaxRetriesInput = document.getElementById("provider-max-retries");
        const providerMaxActiveRequestsInput = document.getElementById("provider-max-active-requests");
        const providerMaxActiveStreamsInput = document.getElementById("provider-max-active-streams");
        const providerMaxQpsInput = document.getElementById("provider-max-qps");
        const providerMaxErrorRateInput = document.getElementById("provider-max-error-rate");
        const providerFirstTokenTimeoutSecInput = document.getElementById("provider-first-token-timeout-sec");
        const providerMaintenanceWindowInput = document.getElementById("provider-maintenance-window");
        const providerMaintenanceModeEnabledInput = document.getElementById("provider-maintenance-mode-enabled");
        const providerAutoCircuitBreakEnabledInput = document.getElementById("provider-auto-circuit-break-enabled");
        const providerAutoRecoverEnabledInput = document.getElementById("provider-auto-recover-enabled");
        const providerCircuitBreakerThresholdOverrideInput = document.getElementById("provider-circuit-breaker-threshold-override");
        const providerRecoveryProbeIntervalOverrideInput = document.getElementById("provider-recovery-probe-interval-override");
        const providerRemarkInput = document.getElementById("provider-remark");
        const providerEnabledInput = document.getElementById("provider-enabled");
        const discoverModelsBtn = document.getElementById("provider-discover-models-btn");
        const loadCatalogModelsBtn = document.getElementById("provider-load-catalog-models-btn");
        const importSelectedModelsBtn = document.getElementById("provider-import-selected-models-btn");
        const discoveredModelsCheckAll = document.getElementById("provider-discovered-models-check-all");
        const discoveredModelsBody = document.getElementById("provider-discovered-models-body");
        const catalogModelShell = document.getElementById("provider-catalog-model-shell");
        const catalogModelMeta = document.getElementById("provider-catalog-model-meta");
        const catalogModelsCheckAll = document.getElementById("provider-catalog-models-check-all");
        const catalogModelsBody = document.getElementById("provider-catalog-models-body");
        const importCatalogModelsBtn = document.getElementById("provider-import-catalog-models-btn");
        const discoverFeedback = document.getElementById("provider-discover-feedback");
        const DEFAULT_PROVIDER_MODEL_CONFIG = {
            supports_stream: true,
            supports_vision: false,
            supports_tools: false,
            enabled: true,
            price_multiplier: 1,
        };
        const DEFAULT_PROVIDER_PRESETS = ["gpt-5.4", "gpt-5", "gpt-5-mini", "gpt-4.1", "gpt-4o", "gpt-4o-mini", "o3", "o4-mini"];
        const PROVIDER_PRESETS_STORAGE_KEY = "aotu_provider_model_presets";
        let providers = [];
        let providerModelItems = [];
        const providerModelState = {
            page: 1,
            pageSize: 20,
            total: 0,
            totalPages: 1,
        };
        let discoveredModels = [];
        let catalogModels = [];
        let providerFormSnapshot = "";
        let providerPresetModels = loadProviderPresetModels();

        if (!tableBody || !modelTableBody || !modal || !providerForm || !providerModelConfigList) return;

        function serializeProviderFormState() {
            return JSON.stringify(
                Array.from(providerForm.querySelectorAll("input, textarea, select")).map((field) => [
                    field.id || field.name || field.type,
                    field.type === "checkbox" ? field.checked : field.value,
                ])
            );
        }

        function updateProviderFormDirtyState() {
            providerForm.dataset.dirty = serializeProviderFormState() === providerFormSnapshot ? "false" : "true";
        }

        function normalizeProviderModelConfig(config = {}) {
            const modelName = String(config.model_name || "").trim();
            return {
                model_name: modelName,
                supports_stream: config.supports_stream ?? DEFAULT_PROVIDER_MODEL_CONFIG.supports_stream,
                supports_vision: config.supports_vision ?? DEFAULT_PROVIDER_MODEL_CONFIG.supports_vision,
                supports_tools: config.supports_tools ?? DEFAULT_PROVIDER_MODEL_CONFIG.supports_tools,
                enabled: config.enabled ?? DEFAULT_PROVIDER_MODEL_CONFIG.enabled,
                price_multiplier: Number.isFinite(Number(config.price_multiplier)) && Number(config.price_multiplier) > 0
                    ? Number(config.price_multiplier)
                    : DEFAULT_PROVIDER_MODEL_CONFIG.price_multiplier,
            };
        }

        function createProviderModelConfigRow(config = {}) {
            const item = normalizeProviderModelConfig(config);
            const rowId = `provider-model-config-${Date.now()}-${Math.random().toString(16).slice(2)}`;
            const row = document.createElement("div");
            row.className = "provider-model-config-row";
            row.dataset.modelConfigRow = "true";
            row.dataset.supportsStream = item.supports_stream ? "true" : "false";
            row.dataset.supportsVision = item.supports_vision ? "true" : "false";
            row.dataset.supportsTools = item.supports_tools ? "true" : "false";
            row.innerHTML = `
                <label class="provider-model-config-name" for="${rowId}-name">
                    <span class="visually-hidden">模型名称</span>
                    <input class="field-input" id="${rowId}-name" data-model-config-field="model_name" value="${escapeHtml(item.model_name)}" placeholder="模型名称，必填" required>
                </label>
                <label class="provider-model-mini-switch settings-switch-control" title="控制该模型是否参与当前中转站路由">
                    <input type="checkbox" data-model-config-field="enabled" ${item.enabled ? "checked" : ""}>
                    <span class="settings-switch-slider" aria-hidden="true"></span>
                </label>
                <label>
                    <span class="visually-hidden">渠道倍率</span>
                    <input class="field-input" type="number" min="0.0001" step="0.0001" data-model-config-field="price_multiplier" value="${item.price_multiplier}" placeholder="倍率">
                </label>
                <button class="table-action-btn" data-action="remove-model-config" type="button">删除</button>
            `;
            return row;
        }

        function renderProviderModelConfigRows(configs = []) {
            providerModelConfigList.innerHTML = "";
            configs.forEach((config) => {
                providerModelConfigList.appendChild(createProviderModelConfigRow(config));
            });
            if (!configs.length) {
                providerModelConfigList.innerHTML = '<div class="empty-state provider-model-config-empty">当前中转站尚未挂载模型，可从上游发现、预设模型或自定义模型添加。</div>';
            }
            enhanceInteractiveButtons(providerModelConfigList);
        }

        function getProviderModelConfigRows() {
            return Array.from(providerModelConfigList.querySelectorAll("[data-model-config-row]"));
        }

        function addProviderModelConfig(config = {}, options = {}) {
            const item = normalizeProviderModelConfig(config);
            if (!item.model_name && !options.allowBlank) {
                showToast("模型名称不能为空", "error");
                return false;
            }
            const existingNameSet = new Set(getCurrentConfiguredModelNames());
            if (item.model_name && existingNameSet.has(item.model_name)) {
                showToast(`模型 ${item.model_name} 已存在`, "error");
                return false;
            }
            providerModelConfigList.querySelector(".provider-model-config-empty")?.remove();
            providerModelConfigList.appendChild(createProviderModelConfigRow(item));
            enhanceInteractiveButtons(providerModelConfigList);
            if (discoveredModels.length) {
                renderDiscoveredModels(discoveredModels);
            }
            if (catalogModels.length) {
                renderCatalogModels(catalogModels);
            }
            updateProviderFormDirtyState();
            return true;
        }

        function collectProviderModelConfigs() {
            const rows = getProviderModelConfigRows();
            const seen = new Set();
            const configs = [];
            for (const row of rows) {
                const modelNameInput = row.querySelector('[data-model-config-field="model_name"]');
                const modelName = String(modelNameInput?.value || "").trim();
                if (!modelName) {
                    modelNameInput?.focus();
                    showToast("模型名称为必填项", "error");
                    return null;
                }
                if (seen.has(modelName)) {
                    modelNameInput?.focus();
                    showToast(`模型 ${modelName} 重复，请删除或修改重复项`, "error");
                    return null;
                }
                seen.add(modelName);
                const priceMultiplierInput = row.querySelector('[data-model-config-field="price_multiplier"]');
                const priceMultiplier = Number(priceMultiplierInput?.value || DEFAULT_PROVIDER_MODEL_CONFIG.price_multiplier);
                if (!Number.isFinite(priceMultiplier) || priceMultiplier <= 0) {
                    priceMultiplierInput?.focus();
                    showToast(`模型 ${modelName} 的倍率必须大于 0`, "error");
                    return null;
                }
                configs.push({
                    model_name: modelName,
                    priority: Number(providerPriorityInput.value || 100),
                    weight: Number(providerWeightInput.value || 100),
                    supports_stream: row.dataset.supportsStream ? row.dataset.supportsStream === "true" : DEFAULT_PROVIDER_MODEL_CONFIG.supports_stream,
                    supports_vision: row.dataset.supportsVision ? row.dataset.supportsVision === "true" : DEFAULT_PROVIDER_MODEL_CONFIG.supports_vision,
                    supports_tools: row.dataset.supportsTools ? row.dataset.supportsTools === "true" : DEFAULT_PROVIDER_MODEL_CONFIG.supports_tools,
                    enabled: row.querySelector('[data-model-config-field="enabled"]')?.checked ?? DEFAULT_PROVIDER_MODEL_CONFIG.enabled,
                    price_multiplier: priceMultiplier,
                });
            }
            return configs;
        }

        function loadProviderPresetModels() {
            try {
                const raw = window.localStorage?.getItem(PROVIDER_PRESETS_STORAGE_KEY);
                const parsed = raw ? JSON.parse(raw) : null;
                if (Array.isArray(parsed)) {
                    const normalized = parsed.map((item) => String(item || "").trim()).filter(Boolean);
                    return Array.from(new Set(normalized)).length ? Array.from(new Set(normalized)) : DEFAULT_PROVIDER_PRESETS;
                }
            } catch (error) {
                console.warn("Failed to load provider presets", error);
            }
            return DEFAULT_PROVIDER_PRESETS;
        }

        function saveProviderPresetModels() {
            try {
                window.localStorage?.setItem(PROVIDER_PRESETS_STORAGE_KEY, JSON.stringify(providerPresetModels));
            } catch (error) {
                console.warn("Failed to save provider presets", error);
            }
        }

        function renderProviderPresetModels() {
            if (presetList) {
                presetList.innerHTML = providerPresetModels.map((modelName) => `
                    <button class="table-action-btn" type="button" data-model-preset="${escapeHtml(modelName)}">${escapeHtml(modelName)}</button>
                `).join("");
                enhanceInteractiveButtons(presetList);
            }
            if (presetEditorList) {
                presetEditorList.innerHTML = providerPresetModels.map((modelName, index) => `
                    <div class="provider-preset-editor-row" data-preset-row="${index}">
                        <label class="visually-hidden" for="provider-preset-${index}">预设模型名</label>
                        <input class="field-input" id="provider-preset-${index}" value="${escapeHtml(modelName)}" data-preset-input="${index}">
                        <button class="table-action-btn" type="button" data-action="save-preset" data-index="${index}">保存</button>
                        <button class="table-action-btn" type="button" data-action="delete-preset" data-index="${index}">删除</button>
                    </div>
                `).join("") || '<div class="empty-state">暂无预设模型，可在下方新增。</div>';
                enhanceInteractiveButtons(presetEditorList);
            }
        }

        const providerModalController = modalManager.register({
            modal,
            dialog: modal.querySelector('[role="dialog"]'),
            closeOnBackdrop: false,
            getInitialFocus: () => providerNameInput,
            beforeClose: () => {
                if (providerForm.dataset.dirty !== "true") return true;
                return window.confirm("表单内容尚未保存，确认关闭吗？");
            },
            afterClose: () => {
                renderDiscoveredModels([]);
                renderCatalogModels([]);
                renderDiscoverFeedback();
                providerForm.dataset.dirty = "false";
            },
        });
        const credentialModalController = modalManager.register({
            modal: credentialModal,
            dialog: credentialModal?.querySelector('[role="dialog"]'),
            getInitialFocus: () => credentialApiKeyInput,
            afterClose: () => {
                credentialForm?.reset();
                credentialProviderIdInput.value = "";
            },
        });
        const modelsDetailModalController = modalManager.register({
            modal: modelsDetailModal,
            dialog: modelsDetailModal?.querySelector('[role="dialog"]'),
            getInitialFocus: () => document.getElementById("provider-models-detail-modal-close"),
            afterClose: () => {
                if (modelsDetailModalContent) {
                    modelsDetailModalContent.innerHTML = "";
                }
            },
        });

        enhanceInteractiveButtons(document);
        document.getElementById("add-provider-btn").addEventListener("click", (event) => openProviderModal(null, event.currentTarget));
        document.getElementById("provider-modal-close").addEventListener("click", closeProviderModal);
        document.getElementById("provider-form-cancel").addEventListener("click", closeProviderModal);
        document.getElementById("provider-test-result-modal-close")?.addEventListener("click", () => closeHealthCheckResultModal());
        document.getElementById("provider-models-detail-modal-close")?.addEventListener("click", closeModelsDetailModal);
        document.getElementById("provider-credential-modal-close")?.addEventListener("click", closeCredentialModal);
        document.getElementById("provider-credential-cancel")?.addEventListener("click", closeCredentialModal);
        renderProviderPresetModels();
        presetList?.addEventListener("click", (event) => {
            const button = event.target.closest("[data-model-preset]");
            if (!button) return;
            const modelName = button.dataset.modelPreset;
            if (addProviderModelConfig({ model_name: modelName })) {
                showToast(`已添加预设模型 ${modelName}`);
            }
        });
        presetManageBtn?.addEventListener("click", () => {
            const willShow = presetManager?.classList.contains("hidden");
            presetManager?.classList.toggle("hidden", !willShow);
            presetManager?.setAttribute("aria-hidden", willShow ? "false" : "true");
            presetManageBtn.setAttribute("aria-expanded", willShow ? "true" : "false");
            if (willShow) {
                renderProviderPresetModels();
                newPresetInput?.focus();
            }
        });
        addPresetBtn?.addEventListener("click", () => {
            const modelName = String(newPresetInput?.value || "").trim();
            if (!modelName) {
                showToast("请先输入预设模型名", "error");
                return;
            }
            if (providerPresetModels.includes(modelName)) {
                showToast(`预设模型 ${modelName} 已存在`, "error");
                return;
            }
            providerPresetModels = [...providerPresetModels, modelName];
            saveProviderPresetModels();
            if (newPresetInput) newPresetInput.value = "";
            renderProviderPresetModels();
            showToast(`已新增预设模型 ${modelName}`);
        });
        presetEditorList?.addEventListener("click", (event) => {
            const button = event.target.closest("[data-action]");
            if (!button) return;
            const index = Number(button.dataset.index);
            if (!Number.isInteger(index) || index < 0 || index >= providerPresetModels.length) return;
            if (button.dataset.action === "delete-preset") {
                const [removed] = providerPresetModels.splice(index, 1);
                saveProviderPresetModels();
                renderProviderPresetModels();
                showToast(`已删除预设模型 ${removed}`);
                return;
            }
            if (button.dataset.action === "save-preset") {
                const input = presetEditorList.querySelector(`[data-preset-input="${index}"]`);
                const nextName = String(input?.value || "").trim();
                if (!nextName) {
                    showToast("预设模型名不能为空", "error");
                    input?.focus();
                    return;
                }
                if (providerPresetModels.some((item, itemIndex) => item === nextName && itemIndex !== index)) {
                    showToast(`预设模型 ${nextName} 已存在`, "error");
                    input?.focus();
                    return;
                }
                providerPresetModels[index] = nextName;
                saveProviderPresetModels();
                renderProviderPresetModels();
                showToast(`已保存预设模型 ${nextName}`);
            }
        });
        addCustomModelBtn.addEventListener("click", () => {
            const modelName = customModelInput.value.trim();
            if (!modelName) {
                showToast("请先输入自定义模型名", "error");
                return;
            }
            if (addProviderModelConfig({ model_name: modelName })) {
                customModelInput.value = "";
                showToast(`已添加自定义模型 ${modelName}`);
            }
        });
        addEmptyModelBtn?.addEventListener("click", () => {
            if (addProviderModelConfig({}, { allowBlank: true })) {
                const rows = getProviderModelConfigRows();
                rows.at(-1)?.querySelector('[data-model-config-field="model_name"]')?.focus();
                setButtonTransientFeedback(addEmptyModelBtn, "success", { successText: "已添加" });
            }
        });
        discoverModelsBtn?.addEventListener("click", async () => {
            await discoverProviderModels();
        });
        loadCatalogModelsBtn?.addEventListener("click", async () => {
            await loadCatalogModels();
        });
        importSelectedModelsBtn?.addEventListener("click", () => {
            const selectedModelNames = getSelectedDiscoveredModelNames();
            if (!selectedModelNames.length) {
                showToast("请先选择至少一个上游模型", "error");
                return;
            }
            importDiscoveredModels(selectedModelNames);
        });
        discoveredModelsCheckAll?.addEventListener("change", () => {
            discoveredModelsBody?.querySelectorAll("[data-discovered-model-name]").forEach((node) => {
                if (!node.disabled) {
                    node.checked = discoveredModelsCheckAll.checked;
                }
            });
            syncDiscoveredCheckAllState();
        });
        discoveredModelsBody?.addEventListener("change", () => {
            syncDiscoveredCheckAllState();
        });
        importCatalogModelsBtn?.addEventListener("click", () => {
            const selectedModelNames = getSelectedCatalogModelNames();
            if (!selectedModelNames.length) {
                showToast("请先选择至少一个模型库模型", "error");
                return;
            }
            importCatalogModels(selectedModelNames);
        });
        catalogModelsCheckAll?.addEventListener("change", () => {
            catalogModelsBody?.querySelectorAll("[data-catalog-model-name]").forEach((node) => {
                if (!node.disabled) {
                    node.checked = catalogModelsCheckAll.checked;
                }
            });
            syncCatalogCheckAllState();
        });
        catalogModelsBody?.addEventListener("change", () => {
            syncCatalogCheckAllState();
        });
        checkAllBtn.addEventListener("click", async () => {
            try {
                setButtonLoading(checkAllBtn, true);
                const results = await runHealthCheckStream({
                    url: "/api/providers/test-all-stream",
                    title: "全部中转站健康检查",
                    scope: "all",
                    trigger: checkAllBtn,
                    showModal: true,
                });
                const providerResults = results.filter((item) => item.scope === "provider");
                const successCount = providerResults.filter((item) => item.success).length;
                setButtonLoading(checkAllBtn, false);
                setButtonTransientFeedback(checkAllBtn, "success", { successText: "已完成" });
                showToast(`已完成全部健康检查：${successCount}/${providerResults.length} 个中转站通过`);
                await loadProviders();
            } catch (error) {
                setButtonLoading(checkAllBtn, false);
                setButtonTransientFeedback(checkAllBtn, "error", { errorText: "失败" });
                showToast(error.message, "error");
            } finally {
                setButtonLoading(checkAllBtn, false);
            }
        });

        providerForm.addEventListener("submit", async (event) => {
            event.preventDefault();
            const id = providerIdInput.value;
            const apiKey = providerApiKeyInput.value.trim();
            if (!id && !apiKey) {
                showToast("新增中转站时必须填写 API Key", "error");
                return;
            }
            const modelConfigs = collectProviderModelConfigs();
            if (modelConfigs === null) return;
            const payload = {
                name: providerNameInput.value.trim(),
                base_url: providerBaseUrlInput.value.trim(),
                provider_type: providerTypeInput.value.trim() || "openai_compatible",
                group_name: providerGroupNameInput.value.trim() || null,
                region_tag: providerRegionTagInput.value.trim() || null,
                enabled: providerEnabledInput.checked,
                priority: Number(providerPriorityInput.value),
                weight: Number(providerWeightInput.value),
                timeout_ms: Number(providerTimeoutMsInput.value),
                max_retries: Number(providerMaxRetriesInput.value),
                max_active_requests: providerMaxActiveRequestsInput.value === "" ? null : Number(providerMaxActiveRequestsInput.value),
                max_active_streams: providerMaxActiveStreamsInput.value === "" ? null : Number(providerMaxActiveStreamsInput.value),
                max_qps: providerMaxQpsInput.value === "" ? null : Number(providerMaxQpsInput.value),
                max_error_rate: providerMaxErrorRateInput.value === "" ? null : Number(providerMaxErrorRateInput.value),
                first_token_timeout_sec: providerFirstTokenTimeoutSecInput.value === "" ? null : Number(providerFirstTokenTimeoutSecInput.value),
                maintenance_window: providerMaintenanceWindowInput.value.trim() || null,
                maintenance_mode_enabled: providerMaintenanceModeEnabledInput.checked,
                auto_circuit_break_enabled: providerAutoCircuitBreakEnabledInput.checked,
                auto_recover_enabled: providerAutoRecoverEnabledInput.checked,
                circuit_breaker_threshold_override: providerCircuitBreakerThresholdOverrideInput.value === "" ? null : Number(providerCircuitBreakerThresholdOverrideInput.value),
                recovery_probe_interval_sec_override: providerRecoveryProbeIntervalOverrideInput.value === "" ? null : Number(providerRecoveryProbeIntervalOverrideInput.value),
                model_configs: modelConfigs,
                remark: providerRemarkInput.value.trim(),
            };
            if (id) {
                if (apiKey) payload.api_key = apiKey;
            } else {
                payload.api_key = apiKey;
            }
            try {
                setButtonLoading(submitBtn, true);
                if (id) {
                    await api.put(`/api/providers/${id}`, payload);
                    showToast("中转站已更新");
                } else {
                    await api.post("/api/providers", payload);
                    showToast("中转站已创建");
                }
                closeProviderModal({ force: true, reason: "submit" });
                await loadProviders();
            } catch (error) {
                showToast(error.message, "error");
            } finally {
                setButtonLoading(submitBtn, false);
                refreshRawApiKeyInputState();
            }
        });

        credentialForm?.addEventListener("submit", async (event) => {
            event.preventDefault();
            const providerId = Number(credentialProviderIdInput.value);
            if (!Number.isFinite(providerId)) return;
            try {
                setButtonLoading(credentialSubmitBtn, true);
                await api.post(`/api/providers/${providerId}/rotate-credential`, {
                    api_key: credentialApiKeyInput.value.trim(),
                    credential_hint: credentialHintInput.value.trim() || null,
                });
                showToast("中转站凭据已轮换");
                closeCredentialModal();
                await loadProviders();
            } catch (error) {
                showToast(error.message, "error");
            } finally {
                setButtonLoading(credentialSubmitBtn, false);
            }
        });

        searchInput.addEventListener("input", () => {
            renderProviders(searchInput.value);
            renderProviderModels(searchInput.value);
        });
        providerForm.addEventListener("input", updateProviderFormDirtyState);
        providerForm.addEventListener("change", updateProviderFormDirtyState);
        providerModelConfigList.addEventListener("input", () => {
            if (discoveredModels.length) {
                renderDiscoveredModels(discoveredModels);
            }
            if (catalogModels.length) {
                renderCatalogModels(catalogModels);
            }
            updateProviderFormDirtyState();
        });
        providerModelConfigList.addEventListener("change", () => {
            if (discoveredModels.length) {
                renderDiscoveredModels(discoveredModels);
            }
            if (catalogModels.length) {
                renderCatalogModels(catalogModels);
            }
            updateProviderFormDirtyState();
        });
        providerModelConfigList.addEventListener("click", (event) => {
            const button = event.target.closest('[data-action="remove-model-config"]');
            if (!button) return;
            button.closest("[data-model-config-row]")?.remove();
            if (!getProviderModelConfigRows().length) {
                renderProviderModelConfigRows([]);
            }
            if (discoveredModels.length) {
                renderDiscoveredModels(discoveredModels);
            }
            if (catalogModels.length) {
                renderCatalogModels(catalogModels);
            }
            updateProviderFormDirtyState();
        });
        availabilityProviderSelect?.addEventListener("change", async () => {
            await loadAvailability({ manual: false });
        });
        availabilityWindowSelect?.addEventListener("change", async () => {
            await loadAvailability({ manual: false });
        });
        availabilityBucketSelect?.addEventListener("change", async () => {
            await loadAvailability({ manual: false });
        });
        availabilityRefreshBtn?.addEventListener("click", async () => {
            await loadAvailability({ manual: true });
        });

        async function loadProviders() {
            providers = await api.get("/api/providers");
            renderProviderTelemetry(providers);
            renderProviders(searchInput.value);
            renderProviderModelFilterOptions();
            populateAvailabilityProviderOptions();
            await loadProviderModels({ silent: true });
            await loadAvailability({ manual: false });
            syncOpenModelsDetailModal();
        }

        function renderProviderTelemetry(currentProviders) {
            const summary = summarizeProviders(currentProviders);
            document.querySelector('[data-provider-stat="provider_count"]').textContent = summary.providerCount;
            document.querySelector('[data-provider-stat="enabled_provider_count"]').textContent = summary.enabledProviderCount;
            document.querySelector('[data-provider-stat="model_count"]').textContent = summary.modelCount;
            document.querySelector('[data-provider-stat="stream_model_count"]').textContent = summary.streamModelCount;
            document.querySelector('[data-provider-stat="vision_model_count"]').textContent = summary.visionModelCount;
            document.querySelector('[data-provider-stat="priced_model_count"]').textContent = summary.pricedModelCount;
            document.querySelector('[data-provider-stat="avg_stability_score"]').textContent = formatScore(summary.avgStabilityScore);

            const healthyRatio = summary.providerCount ? Math.round((summary.healthyProviderCount / summary.providerCount) * 100) : 0;
            const providersSummaryCard = document.getElementById("providers-summary-card");
            if (providersSummaryCard) {
                providersSummaryCard.innerHTML = `
                    <div class="cockpit-aside-label">渠道脉冲</div>
                    <div class="cockpit-aside-value">${summary.enabledProviderCount}</div>
                    <div class="cockpit-aside-copy">当前已启用中转站</div>
                    <div class="cockpit-health-bar"><span style="width:${healthyRatio}%"></span></div>
                    <div class="cockpit-aside-meta">
                        <span>挂载 ${summary.modelCount}</span>
                        <span>支持流式 ${summary.streamModelCount}</span>
                    </div>
                `;
            }
        }

        function renderProviderScope(provider) {
            return `
                <strong>${escapeHtml(provider.group_name || "未分组")}</strong>
                <div class="table-muted">${escapeHtml(provider.region_tag || "未标记地区")}</div>
            `;
        }

        function renderProviderStrategy(provider) {
            const maintenanceText = provider.maintenance_mode_enabled
                ? `维护中 · ${provider.maintenance_window || "未填写维护窗口"}`
                : (provider.maintenance_window || "未设置维护窗口");
            const circuitText = [
                `自动摘除 ${formatSwitchText(provider.auto_circuit_break_enabled)}`,
                `自动恢复 ${formatSwitchText(provider.auto_recover_enabled)}`,
            ].join(" · ");
            const overrideText = [
                provider.circuit_breaker_threshold_override == null ? null : `阈值 ${provider.circuit_breaker_threshold_override}`,
                provider.recovery_probe_interval_sec_override == null ? null : `恢复 ${provider.recovery_probe_interval_sec_override}s`,
            ].filter(Boolean).join(" · ");
            const credentialText = provider.credential_rotated_at
                ? `凭据轮换 ${formatDate(provider.credential_rotated_at)}`
                : "凭据未记录轮换";
            return `
                <strong>${escapeHtml(maintenanceText)}</strong>
                <div class="table-muted">${escapeHtml(circuitText)}</div>
                <div class="table-muted">${escapeHtml(overrideText || credentialText)}</div>
                ${provider.credential_hint ? `<div class="table-muted">${escapeHtml(provider.credential_hint)}</div>` : ""}
            `;
        }

        function formatCapacityLimit(value) {
            return value == null || Number(value) <= 0 ? "不限" : formatNumber(value);
        }

        function renderProviderCapacity(provider) {
            const activeRequests = provider.active_requests ?? 0;
            const activeStreams = provider.active_streams ?? 0;
            const currentQps = provider.current_qps ?? 0;
            const errorLimit = provider.max_error_rate == null ? "不限" : `${provider.max_error_rate}%`;
            return `
                <strong>请求 ${formatNumber(activeRequests)} / ${formatCapacityLimit(provider.max_active_requests)}</strong>
                <div class="table-muted">流式 ${formatNumber(activeStreams)} / ${formatCapacityLimit(provider.max_active_streams)}</div>
                <div class="table-muted">QPS ${formatNumber(currentQps)} / ${formatCapacityLimit(provider.max_qps)}</div>
                <div class="table-muted">失败率上限 ${escapeHtml(errorLimit)} · 首 Token ${provider.first_token_timeout_sec ?? "-"}s</div>
            `;
        }

        function renderProviderModelsDetail(provider) {
            const modelConfigs = Array.isArray(provider?.model_configs) ? provider.model_configs : [];
            const enabledCount = modelConfigs.filter((item) => item.enabled).length;
            const healthyCount = modelConfigs.filter((item) => item.health_status === "healthy").length;
            const streamCount = modelConfigs.filter((item) => item.supports_stream).length;
            const visionCount = modelConfigs.filter((item) => item.supports_vision).length;
            const rows = modelConfigs.map((item) => `
                <tr>
                    <td class="provider-model-name-cell">
                        <strong class="provider-model-detail-name">${escapeHtml(item.model_name)}</strong>
                    </td>
                    <td class="provider-model-status-cell">
                        <div class="provider-model-detail-badges">
                            ${renderStatusWithErrorHint(item.health_status, item.last_error)}
                            <span class="status-badge ${item.enabled ? "status-healthy" : "status-unknown"}">${item.enabled ? "已启用" : "已停用"}</span>
                        </div>
                    </td>
                    <td>${item.supports_stream ? "流式" : "非流式"} / ${item.supports_vision ? "图像" : "文本"} / ${item.supports_tools ? "工具" : "无工具"}</td>
                    <td>
                        <strong>${escapeHtml(item.price_multiplier ?? 1)}x</strong>
                        <div class="table-muted">输入 ${escapeHtml(formatPrice(item.input_price_per_1k))}</div>
                        <div class="table-muted">输出 ${escapeHtml(formatPrice(item.output_price_per_1k))}</div>
                        <div class="table-muted">缓存 ${escapeHtml(formatPrice(item.cache_price_per_1k))}</div>
                    </td>
                    <td>${renderQualitySummary(item)}</td>
                    <td>
                        <button class="table-action-btn" data-action="test-model" data-provider-id="${provider.id}" data-model-id="${item.id}" type="button">测试</button>
                    </td>
                </tr>
            `).join("");
            return `
                <div class="provider-model-detail-shell">
                    <div class="provider-model-detail-provider">
                        <div>
                            <span>中转站</span>
                            <strong>${escapeHtml(provider.name)}</strong>
                        </div>
                        <div>
                            <span>Base URL</span>
                            <strong>${escapeHtml(provider.base_url)}</strong>
                        </div>
                    </div>
                    <div class="provider-model-detail-meta">
                        <div><span>模型总数</span><strong>${formatNumber(modelConfigs.length)}</strong></div>
                        <div><span>已启用</span><strong>${formatNumber(enabledCount)}</strong></div>
                        <div><span>状态正常</span><strong>${formatNumber(healthyCount)}</strong></div>
                        <div><span>流式 / 图像</span><strong>${formatNumber(streamCount)} / ${formatNumber(visionCount)}</strong></div>
                    </div>
                    <div class="table-shell provider-model-detail-table-shell">
                        <table class="data-table provider-model-detail-table">
                            <thead>
                                <tr>
                                    <th>模型</th>
                                    <th>状态</th>
                                    <th>能力</th>
                                    <th>倍率 / 价格</th>
                                    <th>质量</th>
                                    <th>操作</th>
                                </tr>
                            </thead>
                            <tbody>
                                ${rows || '<tr><td colspan="6"><div class="empty-state">当前中转站尚未挂载模型。</div></td></tr>'}
                            </tbody>
                        </table>
                    </div>
                </div>
            `;
        }

        function renderAvailability(items = []) {
            if (!availabilityTableBody) return;
            availabilityTableBody.innerHTML = items.length ? items.map((item) => `
                <tr>
                    <td>${formatDate(item.bucket_start)}</td>
                    <td>${formatNumber(item.total_requests)}</td>
                    <td>${formatNumber(item.success_requests)}</td>
                    <td>${formatNumber(item.failed_requests)}</td>
                    <td>${item.success_rate}%</td>
                    <td>${item.avg_latency_ms ?? "-"} ms</td>
                </tr>
            `).join("") : '<tr><td colspan="6"><div class="empty-state">当前时间窗口内暂无历史请求。</div></td></tr>';
        }

        function getCurrentConfiguredModelNames() {
            return getProviderModelConfigRows()
                .map((row) => String(row.querySelector('[data-model-config-field="model_name"]')?.value || "").trim())
                .filter(Boolean);
        }

        function getSelectedDiscoveredModelNames() {
            if (!discoveredModelsBody) return [];
            return Array.from(discoveredModelsBody.querySelectorAll("[data-discovered-model-name]:checked"))
                .map((input) => String(input.dataset.discoveredModelName || "").trim())
                .filter(Boolean);
        }

        function getSelectedCatalogModelNames() {
            if (!catalogModelsBody) return [];
            return Array.from(catalogModelsBody.querySelectorAll("[data-catalog-model-name]:checked"))
                .map((input) => String(input.dataset.catalogModelName || "").trim())
                .filter(Boolean);
        }

        function syncDiscoveredCheckAllState() {
            if (!discoveredModelsCheckAll) return;
            const total = discoveredModels.filter((item) => !item.already_configured).length;
            const selected = getSelectedDiscoveredModelNames().length;
            discoveredModelsCheckAll.checked = total > 0 && selected === total;
            discoveredModelsCheckAll.indeterminate = selected > 0 && selected < total;
            discoveredModelsCheckAll.disabled = total === 0;
        }

        function renderDiscoveredModels(items = []) {
            if (!discoveredModelsBody) return;
            const configuredNameSet = new Set(getCurrentConfiguredModelNames());
            discoveredModels = items.map((item) => ({
                ...item,
                already_configured: configuredNameSet.has(item.model_name),
            }));
            discoveredModelsBody.innerHTML = discoveredModels.length ? discoveredModels.map((item) => `
                <tr>
                    <td><input type="checkbox" data-discovered-model-name="${escapeHtml(item.model_name)}" ${item.already_configured ? "disabled" : ""}></td>
                    <td>
                        <strong>${escapeHtml(item.model_name)}</strong>
                    </td>
                    <td>${item.supports_stream ? "流式" : "非流式"} / ${item.supports_vision ? "图像" : "文本"} / ${item.supports_tools ? "工具" : "无工具"}</td>
                    <td>${item.already_configured ? "已在当前配置中" : "可导入"}</td>
                </tr>
            `).join("") : '<tr><td colspan="4"><div class="empty-state">当前没有可导入的上游模型。</div></td></tr>';
            syncDiscoveredCheckAllState();
        }

        function normalizeCatalogModel(item = {}) {
            return {
                model_name: String(item.model_name || "").trim(),
                display_name: String(item.display_name || "").trim(),
                enabled: item.enabled !== false,
                supports_stream: item.supports_stream !== false,
                supports_vision: item.supports_vision === true,
                supports_tools: item.supports_tools === true,
                input_price_per_1k: item.input_price_per_1k ?? null,
                output_price_per_1k: item.output_price_per_1k ?? null,
                cache_price_per_1k: item.cache_price_per_1k ?? null,
            };
        }

        function syncCatalogCheckAllState() {
            if (!catalogModelsCheckAll) return;
            const total = Array.from(catalogModelsBody?.querySelectorAll("[data-catalog-model-name]") || [])
                .filter((node) => !node.disabled).length;
            const selected = getSelectedCatalogModelNames().length;
            catalogModelsCheckAll.checked = total > 0 && selected === total;
            catalogModelsCheckAll.indeterminate = selected > 0 && selected < total;
            catalogModelsCheckAll.disabled = total === 0;
        }

        function renderCatalogModels(items = []) {
            if (!catalogModelShell || !catalogModelsBody) return;
            const configuredNameSet = new Set(getCurrentConfiguredModelNames());
            catalogModels = items
                .map(normalizeCatalogModel)
                .filter((item) => item.model_name)
                .sort((left, right) => left.model_name.localeCompare(right.model_name, "zh-CN"));
            const availableModels = catalogModels.filter((item) => !configuredNameSet.has(item.model_name));
            catalogModelShell.classList.toggle("hidden", !catalogModels.length);
            catalogModelShell.setAttribute("aria-hidden", catalogModels.length ? "false" : "true");
            if (catalogModelMeta) {
                catalogModelMeta.textContent = catalogModels.length
                    ? `模型库共 ${formatNumber(catalogModels.length)} 个，当前可添加 ${formatNumber(availableModels.length)} 个。`
                    : "仅展示当前中转站尚未挂载的模型。";
            }
            catalogModelsBody.innerHTML = availableModels.length ? availableModels.map((item) => `
                <tr>
                    <td><input type="checkbox" data-catalog-model-name="${escapeHtml(item.model_name)}"></td>
                    <td>
                        <strong>${escapeHtml(item.model_name)}</strong>
                        ${item.display_name ? `<div class="table-muted">${escapeHtml(item.display_name)}</div>` : ""}
                    </td>
                    <td>${item.supports_stream ? "流式" : "非流式"} / ${item.supports_vision ? "图像" : "文本"} / ${item.supports_tools ? "工具" : "无工具"}</td>
                    <td>
                        <span>输入 ${escapeHtml(formatPrice(item.input_price_per_1k))}</span>
                        <div class="table-muted">输出 ${escapeHtml(formatPrice(item.output_price_per_1k))} · 缓存 ${escapeHtml(formatPrice(item.cache_price_per_1k))}</div>
                    </td>
                    <td>${item.enabled ? "模型库启用" : "模型库停用"}</td>
                </tr>
            `).join("") : '<tr><td colspan="5"><div class="empty-state">模型库中暂无当前中转站未挂载的模型。</div></td></tr>';
            syncCatalogCheckAllState();
        }

        function renderDiscoverFeedback(result) {
            if (!discoverFeedback) return;
            if (!result) {
                discoverFeedback.classList.add("hidden");
                discoverFeedback.classList.remove("is-error");
                discoverFeedback.innerHTML = "";
                return;
            }
            const isError = result.type === "error";
            discoverFeedback.classList.toggle("is-error", isError);
            discoverFeedback.classList.remove("hidden");
            discoverFeedback.innerHTML = `
                <strong>${escapeHtml(result.title)}</strong>
                <span>${escapeHtml(result.message)}</span>
            `;
        }

        async function discoverProviderModels() {
            const payload = {
                provider_id: providerIdInput.value === "" ? null : Number(providerIdInput.value),
                base_url: providerBaseUrlInput.value.trim() || null,
                api_key: providerApiKeyInput.value.trim() || null,
                provider_type: providerTypeInput.value.trim() || null,
                timeout_ms: providerTimeoutMsInput.value === "" ? null : Number(providerTimeoutMsInput.value),
                existing_model_names: getCurrentConfiguredModelNames(),
            };
            try {
                setButtonLoading(discoverModelsBtn, true);
                renderDiscoverFeedback({
                    type: "success",
                    title: "正在获取上游模型",
                    message: "正在请求上游 /models，请稍候。",
                });
                const data = await api.post("/api/providers/discover-models", payload);
                const items = Array.isArray(data.items) ? data.items : [];
                const modelNames = items.map((item) => item.model_name).filter(Boolean);
                renderDiscoveredModels(items);
                renderDiscoverFeedback({
                    type: "success",
                    title: `获取成功，共 ${formatNumber(modelNames.length)} 个模型`,
                    message: modelNames.length ? modelNames.join("、") : "上游返回为空，当前没有可导入的模型。",
                });
                showToast(`已获取 ${formatNumber(modelNames.length)} 个可用模型`);
            } catch (error) {
                renderDiscoveredModels([]);
                renderDiscoverFeedback({
                    type: "error",
                    title: "获取可用模型失败",
                    message: error.message,
                });
                window.alert(`获取可用模型失败：${error.message}`);
                showToast(error.message, "error");
            } finally {
                setButtonLoading(discoverModelsBtn, false);
            }
        }

        function importDiscoveredModels(modelNames) {
            let addedCount = 0;
            modelNames.forEach((modelName) => {
                if (addProviderModelConfig({ model_name: modelName })) {
                    addedCount += 1;
                }
            });
            renderDiscoveredModels(discoveredModels);
            if (addedCount > 0) {
                showToast(`已添加 ${formatNumber(addedCount)} 个模型到当前中转站挂载列表`);
            }
        }

        async function loadCatalogModels() {
            try {
                setButtonLoading(loadCatalogModelsBtn, true);
                if (catalogModelShell) {
                    catalogModelShell.classList.remove("hidden");
                    catalogModelShell.setAttribute("aria-hidden", "false");
                }
                if (catalogModelMeta) {
                    catalogModelMeta.textContent = "正在读取模型管理中的模型。";
                }
                const data = await api.get("/api/models");
                const items = Array.isArray(data) ? data : (Array.isArray(data.items) ? data.items : []);
                renderCatalogModels(items);
                showToast(`已读取 ${formatNumber(items.length)} 个模型库模型`);
            } catch (error) {
                renderCatalogModels([]);
                if (catalogModelShell) {
                    catalogModelShell.classList.remove("hidden");
                    catalogModelShell.setAttribute("aria-hidden", "false");
                }
                if (catalogModelsBody) {
                    catalogModelsBody.innerHTML = `<tr><td colspan="5"><div class="empty-state">读取模型库失败：${escapeHtml(error.message)}</div></td></tr>`;
                }
                if (catalogModelMeta) {
                    catalogModelMeta.textContent = "读取模型库失败。";
                }
                syncCatalogCheckAllState();
                showToast(error.message, "error");
            } finally {
                setButtonLoading(loadCatalogModelsBtn, false);
            }
        }

        function importCatalogModels(modelNames) {
            const catalogByName = new Map(catalogModels.map((item) => [item.model_name, item]));
            let addedCount = 0;
            modelNames.forEach((modelName) => {
                const catalogModel = catalogByName.get(modelName) || {};
                if (addProviderModelConfig({
                    model_name: modelName,
                    supports_stream: catalogModel.supports_stream,
                    supports_vision: catalogModel.supports_vision,
                    supports_tools: catalogModel.supports_tools,
                    enabled: DEFAULT_PROVIDER_MODEL_CONFIG.enabled,
                    price_multiplier: DEFAULT_PROVIDER_MODEL_CONFIG.price_multiplier,
                })) {
                    addedCount += 1;
                }
            });
            renderCatalogModels(catalogModels);
            if (addedCount > 0) {
                showToast(`已从模型库添加 ${formatNumber(addedCount)} 个模型`);
            }
        }

        function populateAvailabilityProviderOptions(selectedProviderId = availabilityProviderSelect?.value || "") {
            if (!availabilityProviderSelect) return;
            availabilityProviderSelect.innerHTML = '<option value="">请选择一个中转站</option>' + providers.map((provider) => `
                <option value="${provider.id}" ${String(provider.id) === String(selectedProviderId) ? "selected" : ""}>
                    ${escapeHtml(provider.name)}${provider.group_name ? ` · ${escapeHtml(provider.group_name)}` : ""}
                </option>
            `).join("");
            if (!availabilityProviderSelect.value && providers.length) {
                availabilityProviderSelect.value = String(providers[0].id);
            }
        }

        function renderProviderModelFilterOptions(selectedProviderId = providerModelProviderSelect?.value || "") {
            if (!providerModelProviderSelect) return;
            providerModelProviderSelect.innerHTML = '<option value="">全部中转站</option>' + providers.map((provider) => `
                <option value="${provider.id}" ${String(provider.id) === String(selectedProviderId) ? "selected" : ""}>
                    ${escapeHtml(provider.name)}${provider.group_name ? ` · ${escapeHtml(provider.group_name)}` : ""}
                </option>
            `).join("");
        }

        function buildProviderModelListParams() {
            const params = new URLSearchParams({
                page: String(providerModelState.page),
                page_size: String(providerModelState.pageSize),
            });
            const keyword = providerModelSearchInput?.value.trim();
            if (keyword) params.set("keyword", keyword);
            if (providerModelProviderSelect?.value) params.set("provider_id", providerModelProviderSelect.value);
            if (providerModelEnabledSelect?.value) params.set("enabled", providerModelEnabledSelect.value);
            if (providerModelHealthSelect?.value) params.set("health_status", providerModelHealthSelect.value);
            return params;
        }

        function renderProviderModelPagination() {
            providerModelState.totalPages = Math.max(1, Number(providerModelState.totalPages || Math.ceil((providerModelState.total || 0) / providerModelState.pageSize) || 1));
            providerModelState.page = Math.min(Math.max(1, Number(providerModelState.page || 1)), providerModelState.totalPages);
            if (providerModelPageMeta) {
                providerModelPageMeta.textContent = `第 ${formatNumber(providerModelState.page)} 页，共 ${formatNumber(providerModelState.totalPages)} 页 · 共 ${formatNumber(providerModelState.total || 0)} 条`;
            }
            if (providerModelPrevPageBtn) providerModelPrevPageBtn.disabled = providerModelState.page <= 1;
            if (providerModelNextPageBtn) providerModelNextPageBtn.disabled = providerModelState.page >= providerModelState.totalPages;
        }

        async function reloadProviderModelFirstPage() {
            providerModelState.page = 1;
            await loadProviderModels({ silent: true });
        }

        async function loadProviderModels({ silent = false } = {}) {
            const result = await api.get(`/api/providers/models?${buildProviderModelListParams().toString()}`);
            providerModelItems = Array.isArray(result.items) ? result.items : [];
            providerModelState.total = Number(result.total || 0);
            providerModelState.page = Number(result.page || providerModelState.page || 1);
            providerModelState.pageSize = Number(result.page_size || providerModelState.pageSize || 20);
            providerModelState.totalPages = Number(result.total_pages || 1);
            if (providerModelPageSizeSelect) providerModelPageSizeSelect.value = String(providerModelState.pageSize);
            renderProviderModels(providerModelItems);
            renderProviderModelPagination();
            if (!silent) showToast("模型挂载矩阵已刷新");
        }

        async function loadAvailability({ manual = false } = {}) {
            if (!availabilityProviderSelect || !availabilityWindowSelect || !availabilityBucketSelect || !availabilityTableBody) return;
            const providerId = Number(availabilityProviderSelect.value);
            if (!Number.isFinite(providerId)) {
                renderAvailability([]);
                return;
            }
            try {
                if (manual) setButtonLoading(availabilityRefreshBtn, true);
                const data = await api.get(`/api/providers/${providerId}/availability?window_hours=${encodeURIComponent(availabilityWindowSelect.value)}&bucket_minutes=${encodeURIComponent(availabilityBucketSelect.value)}`);
                renderAvailability(Array.isArray(data.items) ? data.items : []);
                if (manual) showToast(`已刷新 ${data.provider_name} 的历史可用率`);
            } catch (error) {
                renderAvailability([]);
                if (manual) showToast(error.message, "error");
            } finally {
                if (manual) setButtonLoading(availabilityRefreshBtn, false);
            }
        }

        function renderProviders(keyword = "") {
            const query = keyword.trim().toLowerCase();
            const filtered = providers.filter((provider) => {
                if (!query) return true;
                const text = [
                    provider.name,
                    provider.group_name || "",
                    provider.region_tag || "",
                    provider.base_url,
                    provider.models.join(", "),
                    provider.maintenance_window || "",
                    provider.credential_hint || "",
                    provider.remark || "",
                ].join(" ").toLowerCase();
                return text.includes(query);
            });
            tableBody.innerHTML = filtered.map((provider) => `
                <tr>
                    <td>
                        <strong>${escapeHtml(provider.name)}</strong>
                        <div class="provider-key-row">
                            <span class="table-muted">API Key ${escapeHtml(provider.api_key_masked)}</span>
                            <button class="table-action-btn provider-key-copy-btn" type="button" data-copy-text="${escapeHtml(provider.api_key || "")}" ${provider.api_key ? "" : "disabled"}>复制</button>
                        </div>
                    </td>
                    <td>${renderProviderScope(provider)}</td>
                    <td>${escapeHtml(provider.base_url)}</td>
                    <td>${renderProviderModelHealth(provider.model_configs, provider.id)}</td>
                    <td>${statusBadge(provider.health_status)}</td>
                    <td>${statusBadge(provider.circuit_state)}</td>
                    <td>${renderProviderStrategy(provider)}</td>
                    <td>${renderProviderCapacity(provider)}</td>
                    <td>${renderQualitySummary(provider)}</td>
                    <td>${provider.priority}</td>
                    <td>${provider.weight}</td>
                    <td>
                        <div class="table-actions">
                            <button class="table-action-btn" data-action="edit" data-id="${provider.id}">编辑</button>
                            <button class="table-action-btn" data-action="test" data-id="${provider.id}">测试</button>
                            <button class="table-action-btn" data-action="rotate-credential" data-id="${provider.id}">轮换凭据</button>
                            <button class="table-action-btn" data-action="default" data-id="${provider.id}">设为默认</button>
                            <button class="table-action-btn" data-action="toggle" data-id="${provider.id}">${provider.enabled ? "禁用" : "启用"}</button>
                            <button class="table-action-btn" data-action="delete" data-id="${provider.id}">删除</button>
                        </div>
                    </td>
                </tr>
            `).join("") || '<tr><td colspan="12"><div class="empty-state">没有匹配的中转站</div></td></tr>';
            enhanceInteractiveButtons(tableBody);
        }

        function getProviderModelContext(providerId, modelId) {
            const mountedItem = providerModelItems.find((item) => item.provider?.id === providerId && item.model?.id === modelId);
            if (mountedItem) {
                return { owner: mountedItem.provider, modelConfig: mountedItem.model };
            }
            const owner = providers.find((item) => item.id === providerId);
            const modelConfig = owner?.model_configs?.find((item) => item.id === modelId);
            return { owner, modelConfig };
        }

        function applyProviderModelHealthSnapshot(providerId, modelId, snapshot = {}) {
            const nextHealthStatus = snapshot.health_status || "unknown";
            const nextLastError = snapshot.last_error === undefined
                ? (snapshot.success ? null : (snapshot.message || null))
                : snapshot.last_error;
            providers.forEach((provider) => {
                if (provider.id !== providerId || !Array.isArray(provider.model_configs)) return;
                provider.model_configs.forEach((modelConfig) => {
                    if (modelConfig.id !== modelId) return;
                    modelConfig.health_status = nextHealthStatus;
                    modelConfig.last_error = nextLastError;
                    if (snapshot.latency_ms != null) {
                        modelConfig.last_latency_ms = snapshot.latency_ms;
                    }
                });
            });
            providerModelItems.forEach((item) => {
                if (item.provider?.id !== providerId || item.model?.id !== modelId) return;
                item.model.health_status = nextHealthStatus;
                item.model.last_error = nextLastError;
                if (snapshot.latency_ms != null) {
                    item.model.last_latency_ms = snapshot.latency_ms;
                }
            });
            renderProviders(searchInput.value);
            renderProviderModels(providerModelItems);
            syncOpenModelsDetailModal();
        }

        function syncOpenModelsDetailModal() {
            const providerId = Number(modelsDetailModal?.dataset.providerId || "");
            if (!modelsDetailModalController?.isOpen?.() || !Number.isFinite(providerId)) return;
            const provider = providers.find((item) => item.id === providerId);
            if (!provider || !modelsDetailModalTitle || !modelsDetailModalContent) return;
            modelsDetailModalTitle.textContent = `中转站模型 · ${provider.name}`;
            modelsDetailModalContent.innerHTML = renderProviderModelsDetail(provider);
            enhanceInteractiveButtons(modelsDetailModalContent);
        }

        function renderProviderModels(items = []) {
            modelTableBody.innerHTML = items.map((item) => {
                const provider = item.provider || {};
                const model = item.model || {};
                return `
                <tr>
                    <td>${escapeHtml(provider.name)}</td>
                    <td class="provider-model-name-cell">
                        <strong>${escapeHtml(model.model_name)}</strong>
                    </td>
                    <td class="provider-model-status-cell">${renderStatusWithErrorHint(model.health_status, model.last_error)}</td>
                    <td>${model.supports_stream ? "流式" : "非流式"} / ${model.supports_vision ? "图像" : "文本"} / ${model.supports_tools ? "工具" : "无工具"}</td>
                    <td>
                        <input class="field-input" type="number" min="0.0001" step="0.0001" value="${model.price_multiplier ?? 1}" placeholder="渠道倍率" data-model-field="price_multiplier" data-provider-id="${provider.id}" data-model-id="${model.id}">
                        <div class="table-muted">输入 ${escapeHtml(formatPrice(model.input_price_per_1k))}</div>
                        <div class="table-muted">输出 ${escapeHtml(formatPrice(model.output_price_per_1k))}</div>
                        <div class="table-muted">缓存 ${escapeHtml(formatPrice(model.cache_price_per_1k))}</div>
                    </td>
                    <td>${renderQualitySummary(model)}</td>
                    <td>
                        <label class="toggle-row">
                            <input type="checkbox" ${model.enabled ? "checked" : ""} data-model-field="enabled" data-provider-id="${provider.id}" data-model-id="${model.id}">
                            <span>${model.enabled ? "启用" : "停用"}</span>
                        </label>
                    </td>
                    <td>
                        <div class="table-actions">
                            <button class="table-action-btn" data-action="save-model" data-provider-id="${provider.id}" data-model-id="${model.id}">保存</button>
                            <button class="table-action-btn" data-action="test-model" data-provider-id="${provider.id}" data-model-id="${model.id}">测试</button>
                            <button class="table-action-btn" data-action="toggle-model" data-provider-id="${provider.id}" data-model-id="${model.id}">${model.enabled ? "停用" : "启用"}</button>
                        </div>
                    </td>
                </tr>
            `;
            }).join("") || '<tr><td colspan="8"><div class="empty-state">当前筛选条件下没有模型挂载记录</div></td></tr>';
            enhanceInteractiveButtons(modelTableBody);
        }

        async function testProviderModel(providerId, modelId, trigger, options = {}) {
            const { owner, modelConfig } = getProviderModelContext(providerId, modelId);
            if (!owner || !modelConfig) return;
            setButtonLoading(trigger, true);
            try {
                const result = await api.post(`/api/providers/${providerId}/models/${modelId}/test`, {});
                applyProviderModelHealthSnapshot(providerId, modelId, {
                    success: result.success === true,
                    health_status: result.health_status,
                    last_error: result.success ? null : (result.message || null),
                    latency_ms: result.latency_ms,
                    message: result.message,
                });
                setButtonLoading(trigger, false);
                setButtonTransientFeedback(trigger, result.success ? "success" : "error", {
                    successText: "成功",
                    errorText: "失败",
                });
                showToast(
                    formatTestResultLabel(result, `模型 ${modelConfig.model_name}`),
                    result.success ? "success" : "error",
                );
                if (options.closeModelsDetail) {
                    closeModelsDetailModal({ force: true, reason: "test-model" });
                }
                openHealthCheckResultModal(
                    `模型测试结果 · ${modelConfig.model_name}`,
                    renderProviderTestModalBody(result, { scope: "model", name: `${owner.name} / ${modelConfig.model_name}` }),
                    trigger,
                );
                await loadProviders();
            } catch (error) {
                setButtonLoading(trigger, false);
                setButtonTransientFeedback(trigger, "error", { errorText: "失败" });
                showToast(error.message, "error");
            } finally {
                setButtonLoading(trigger, false);
            }
        }

        tableBody.addEventListener("click", async (event) => {
            const button = event.target.closest("button[data-action]");
            if (!button) return;
            const action = button.dataset.action;
            const id = Number(button.dataset.id);
            const provider = providers.find((item) => item.id === id);
            if (!provider) return;
            try {
                if (action === "edit") {
                    openProviderModal(provider, button);
                    return;
                }
                if (action === "rotate-credential") {
                    openCredentialModal(provider, button);
                    return;
                }
                if (action === "view-models") {
                    openModelsDetailModal(provider, button);
                    return;
                }
                if (action === "test") {
                    setButtonLoading(button, true);
                    const result = await runHealthCheckStream({
                        url: `/api/providers/${id}/test-stream`,
                        title: `中转站测试结果 · ${provider.name}`,
                        scope: "provider",
                        trigger: button,
                        showModal: true,
                    });
                    setButtonLoading(button, false);
                    setButtonTransientFeedback(button, result.success ? "success" : "error", {
                        successText: "成功",
                        errorText: "失败",
                    });
                    showToast(
                        formatTestResultLabel(result, provider.name),
                        result.success ? "success" : "error",
                    );
                    await wait(650);
                    await loadProviders();
                    return;
                }
                if (action === "toggle") {
                    setButtonLoading(button, true);
                    await api.put(`/api/providers/${id}`, { enabled: !provider.enabled });
                    showToast(`${provider.enabled ? "已禁用" : "已启用"} ${provider.name}`);
                    await loadProviders();
                    return;
                }
                if (action === "delete") {
                    if (!window.confirm(`确认删除 ${provider.name} 吗？`)) return;
                    setButtonLoading(button, true);
                    await api.delete(`/api/providers/${id}`);
                    showToast("已删除中转站");
                    await loadProviders();
                    return;
                }
                if (action === "default") {
                    setButtonLoading(button, true);
                    const settings = await api.get("/api/settings");
                    await api.put("/api/settings", { ...settings, default_provider_id: id });
                    showToast(`默认中转已切换为 ${provider.name}`);
                    await loadProviders();
                }
            } catch (error) {
                if (action === "test") {
                    setButtonTransientFeedback(button, "error", { errorText: "失败" });
                }
                showToast(error.message, "error");
            } finally {
                setButtonLoading(button, false);
            }
        });

        modelTableBody.addEventListener("click", async (event) => {
            const button = event.target.closest("button[data-action]");
            if (!button) return;
            const action = button.dataset.action;
            const providerId = Number(button.dataset.providerId);
            const modelId = Number(button.dataset.modelId);
            const { owner, modelConfig } = getProviderModelContext(providerId, modelId);
            if (!owner || !modelConfig) return;

            if (action === "test-model") {
                await testProviderModel(providerId, modelId, button);
                return;
            }

            if (action === "save-model" || action === "toggle-model") {
                const enabledInput = modelTableBody.querySelector(`input[data-model-field="enabled"][data-provider-id="${providerId}"][data-model-id="${modelId}"]`);
                const priceMultiplierInput = modelTableBody.querySelector(`input[data-model-field="price_multiplier"][data-provider-id="${providerId}"][data-model-id="${modelId}"]`);
                const payload = {
                    enabled: action === "toggle-model" ? !modelConfig.enabled : enabledInput.checked,
                    price_multiplier: Number(priceMultiplierInput.value || 1),
                };
                try {
                    setButtonLoading(button, true);
                    await api.put(`/api/providers/${providerId}/models/${modelId}`, payload);
                    showToast(`已更新模型 ${modelConfig.model_name}`);
                    await loadProviders();
                } catch (error) {
                    showToast(error.message, "error");
                } finally {
                    setButtonLoading(button, false);
                }
            }
        });

        let providerModelSearchTimer = 0;
        providerModelSearchInput?.addEventListener("input", () => {
            window.clearTimeout(providerModelSearchTimer);
            providerModelSearchTimer = window.setTimeout(async () => {
                try {
                    await reloadProviderModelFirstPage();
                } catch (error) {
                    showToast(error.message, "error");
                }
            }, 250);
        });
        [providerModelProviderSelect, providerModelEnabledSelect, providerModelHealthSelect].forEach((field) => {
            field?.addEventListener("change", async () => {
                try {
                    await reloadProviderModelFirstPage();
                } catch (error) {
                    showToast(error.message, "error");
                }
            });
        });
        providerModelPageSizeSelect?.addEventListener("change", async () => {
            providerModelState.pageSize = Number.parseInt(providerModelPageSizeSelect.value || "20", 10) || 20;
            try {
                await reloadProviderModelFirstPage();
            } catch (error) {
                showToast(error.message, "error");
            }
        });
        providerModelPrevPageBtn?.addEventListener("click", async () => {
            if (providerModelState.page <= 1) return;
            providerModelState.page -= 1;
            try {
                await loadProviderModels({ silent: true });
            } catch (error) {
                showToast(error.message, "error");
            }
        });
        providerModelNextPageBtn?.addEventListener("click", async () => {
            if (providerModelState.page >= providerModelState.totalPages) return;
            providerModelState.page += 1;
            try {
                await loadProviderModels({ silent: true });
            } catch (error) {
                showToast(error.message, "error");
            }
        });

        function openProviderModal(provider, trigger = document.activeElement) {
            document.getElementById("provider-modal-title").textContent = provider ? "编辑中转站" : "新增中转站";
            providerIdInput.value = provider?.id ?? "";
            providerNameInput.value = provider?.name ?? "";
            providerBaseUrlInput.value = provider?.base_url ?? "";
            providerApiKeyInput.value = provider?.api_key ?? "";
            providerTypeInput.value = provider?.provider_type ?? "openai_compatible";
            providerGroupNameInput.value = provider?.group_name ?? "";
            providerRegionTagInput.value = provider?.region_tag ?? "";
            providerPriorityInput.value = provider?.priority ?? 100;
            providerWeightInput.value = provider?.weight ?? 100;
            providerTimeoutMsInput.value = provider?.timeout_ms ?? 30000;
            providerMaxRetriesInput.value = provider?.max_retries ?? 1;
            providerMaxActiveRequestsInput.value = provider?.max_active_requests ?? 300;
            providerMaxActiveStreamsInput.value = provider?.max_active_streams ?? 150;
            providerMaxQpsInput.value = provider?.max_qps ?? "";
            providerMaxErrorRateInput.value = provider?.max_error_rate ?? 80;
            providerFirstTokenTimeoutSecInput.value = provider?.first_token_timeout_sec ?? 60;
            providerMaintenanceWindowInput.value = provider?.maintenance_window ?? "";
            providerMaintenanceModeEnabledInput.checked = provider?.maintenance_mode_enabled ?? false;
            providerAutoCircuitBreakEnabledInput.checked = provider?.auto_circuit_break_enabled ?? true;
            providerAutoRecoverEnabledInput.checked = provider?.auto_recover_enabled ?? true;
            providerCircuitBreakerThresholdOverrideInput.value = provider?.circuit_breaker_threshold_override ?? "";
            providerRecoveryProbeIntervalOverrideInput.value = provider?.recovery_probe_interval_sec_override ?? "";
            renderProviderModelConfigRows(provider?.model_configs ?? []);
            customModelInput.value = "";
            if (presetManager) {
                presetManager.classList.add("hidden");
                presetManager.setAttribute("aria-hidden", "true");
            }
            presetManageBtn?.setAttribute("aria-expanded", "false");
            providerRemarkInput.value = provider?.remark ?? "";
            providerEnabledInput.checked = provider?.enabled ?? true;
            renderDiscoveredModels([]);
            providerFormSnapshot = serializeProviderFormState();
            providerForm.dataset.dirty = "false";
            providerModalController.open(trigger);
        }

        function closeProviderModal(options = {}) {
            providerModalController.close(options);
        }

        function openCredentialModal(provider, trigger = document.activeElement) {
            if (!credentialModal) return;
            credentialProviderIdInput.value = provider.id;
            credentialApiKeyInput.value = "";
            credentialHintInput.value = provider.credential_hint || "";
            document.getElementById("provider-credential-modal-title").textContent = `轮换凭据 · ${provider.name}`;
            credentialModalController.open(trigger);
        }

        function closeCredentialModal(options = {}) {
            credentialModalController.close(options);
        }

        function openModelsDetailModal(provider, trigger = document.activeElement) {
            if (!modelsDetailModal || !modelsDetailModalTitle || !modelsDetailModalContent) return;
            modelsDetailModal.dataset.providerId = String(provider.id);
            modelsDetailModalTitle.textContent = `中转站模型 · ${provider.name}`;
            modelsDetailModalContent.innerHTML = renderProviderModelsDetail(provider);
            enhanceInteractiveButtons(modelsDetailModalContent);
            modelsDetailModalController.open(trigger);
        }

        function closeModelsDetailModal(options = {}) {
            if (modelsDetailModal) {
                delete modelsDetailModal.dataset.providerId;
            }
            modelsDetailModalController.close(options);
        }

        modelsDetailModalContent?.addEventListener("click", async (event) => {
            const button = event.target.closest('button[data-action="test-model"]');
            if (!button) return;
            await testProviderModel(
                Number(button.dataset.providerId),
                Number(button.dataset.modelId),
                button,
                { closeModelsDetail: true },
            );
        });

        let providerPageRefreshRunning = false;
        const providerPageTimer = window.setInterval(async () => {
            if (document.hidden || providerPageRefreshRunning) return;
            providerPageRefreshRunning = true;
            try {
                await loadProviders();
            } catch (error) {
                console.error(error);
            } finally {
                providerPageRefreshRunning = false;
            }
        }, 30000);
        window.addEventListener("beforeunload", () => window.clearInterval(providerPageTimer), { once: true });

        await loadProviders();
    }

    async function initModels() {
        const tableBody = document.getElementById("models-table-body");
        const searchInput = document.getElementById("models-search");
        const enabledSelect = document.getElementById("models-enabled");
        const providerSelect = document.getElementById("models-provider-id");
        const pageSizeSelect = document.getElementById("models-page-size");
        const pageMeta = document.getElementById("models-page-meta");
        const prevPageBtn = document.getElementById("models-prev-page-btn");
        const nextPageBtn = document.getElementById("models-next-page-btn");
        const selectPageInput = document.getElementById("models-select-page");
        const batchMeta = document.getElementById("models-batch-meta");
        const batchContextWindowInput = document.getElementById("models-batch-context-window-tokens");
        const batchContextApplyBtn = document.getElementById("models-batch-context-apply-btn");
        const refreshBtn = document.getElementById("models-refresh-btn");
        const addBtn = document.getElementById("add-model-btn");
        const modal = document.getElementById("model-modal");
        const modalTitle = document.getElementById("model-modal-title");
        const closeBtn = document.getElementById("model-modal-close");
        const cancelBtn = document.getElementById("model-form-cancel");
        const form = document.getElementById("model-form");
        const submitBtn = document.getElementById("model-submit-btn");
        const nameInput = document.getElementById("model-name");
        const displayNameInput = document.getElementById("model-display-name");
        const enabledInput = document.getElementById("model-enabled");
        const supportsStreamInput = document.getElementById("model-supports-stream");
        const supportsVisionInput = document.getElementById("model-supports-vision");
        const supportsToolsInput = document.getElementById("model-supports-tools");
        const supportsChatCompletionsInput = document.getElementById("model-supports-chat-completions");
        const supportsResponsesInput = document.getElementById("model-supports-responses");
        const contextWindowInput = document.getElementById("model-context-window-tokens");
        const maxInputTokensInput = document.getElementById("model-max-input-tokens");
        const maxOutputTokensInput = document.getElementById("model-max-output-tokens");
        const inputPriceInput = document.getElementById("model-input-price");
        const outputPriceInput = document.getElementById("model-output-price");
        const cachePriceInput = document.getElementById("model-cache-price");
        const speedLabelInput = document.getElementById("model-speed-label");
        const remarkInput = document.getElementById("model-remark");
        const bindingBody = document.getElementById("model-binding-body");
        if (
            !tableBody || !searchInput || !enabledSelect || !providerSelect || !pageSizeSelect || !cachePriceInput
            || !supportsStreamInput || !supportsVisionInput || !supportsToolsInput || !supportsChatCompletionsInput
            || !supportsResponsesInput || !contextWindowInput || !maxInputTokensInput || !maxOutputTokensInput
            || !selectPageInput || !batchMeta || !batchContextWindowInput || !batchContextApplyBtn
            || !pageMeta || !prevPageBtn || !nextPageBtn || !refreshBtn || !addBtn || !modal || !form || !bindingBody
        ) return;

        const state = {
            models: [],
            providers: [],
            editingModelName: null,
            page: 1,
            pageSize: 20,
            total: 0,
            totalPages: 1,
            selectedModelNames: new Set(),
        };
        let searchTimer = null;

        function updateSummary(summary = {}) {
            const normalizedSummary = {
                total: summary.total ?? 0,
                enabled: summary.enabled ?? 0,
                boundProviders: summary.bound_providers ?? summary.boundProviders ?? 0,
                availableProviders: summary.available_providers ?? summary.availableProviders ?? summary.enabled_providers ?? summary.enabledProviders ?? 0,
            };
            document.querySelectorAll("[data-model-summary]").forEach((node) => {
                node.textContent = normalizedSummary[node.dataset.modelSummary] ?? "0";
            });
        }

        function renderProviderFilterOptions() {
            const currentValue = providerSelect.value;
            providerSelect.innerHTML = '<option value="">全部中转站</option>' + state.providers.map((provider) => `
                <option value="${provider.id}">${escapeHtml(provider.name)}${provider.enabled ? "" : "（已停用）"}</option>
            `).join("");
            if (currentValue && Array.from(providerSelect.options).some((option) => option.value === currentValue)) {
                providerSelect.value = currentValue;
            }
        }

        function formatMultiplier(value) {
            return value == null ? "-" : `${Number(value).toFixed(2)}x`;
        }

        function renderMultiplierCell(item) {
            const boundAverage = item.avg_bound_price_multiplier ?? item.avg_price_multiplier;
            const routableAverage = item.avg_routable_price_multiplier;
            const boundCount = Number(item.bound_price_multiplier_count ?? item.provider_count ?? 0);
            const routableCount = Number(item.routable_price_multiplier_count ?? item.enabled_provider_count ?? 0);
            const rangeText = item.min_bound_price_multiplier == null || item.max_bound_price_multiplier == null
                ? "无倍率范围"
                : `${formatMultiplier(item.min_bound_price_multiplier)} - ${formatMultiplier(item.max_bound_price_multiplier)}`;
            return `
                <strong>${formatMultiplier(boundAverage)}</strong>
                <div class="table-muted">已绑定 ${escapeHtml(String(boundCount))} 个 · 范围 ${escapeHtml(rangeText)}</div>
                <div class="table-muted">可路由平均 ${escapeHtml(formatMultiplier(routableAverage))} · ${escapeHtml(String(routableCount))} 个</div>
            `;
        }

        function formatContextWindow(value) {
            return value == null ? "未设置" : formatNumber(value);
        }

        function renderTokenLimitCell(item) {
            return `
                <div>上下文（Token） ${escapeHtml(formatContextWindow(item.context_window_tokens))}</div>
                <div class="table-muted">输入（Token） ${escapeHtml(formatContextWindow(item.max_input_tokens))}</div>
                <div class="table-muted">输出（Token） ${escapeHtml(formatContextWindow(item.max_output_tokens))}</div>
            `;
        }

        function renderModelAbilityCell(item) {
            const endpointLabels = [];
            if (item.supports_chat_completions) endpointLabels.push("Chat");
            if (item.supports_responses) endpointLabels.push("Responses");
            return `
                <div>${item.supports_stream ? "流式" : "非流式"} / ${item.supports_vision ? "图像" : "文本"} / ${item.supports_tools ? "工具" : "无工具"}</div>
                <div class="table-muted">${escapeHtml(endpointLabels.join("、") || "未配置原生端点")}</div>
            `;
        }

        function updateBatchBar() {
            const selectedCount = state.selectedModelNames.size;
            batchMeta.textContent = `已选 ${formatNumber(selectedCount)} 个模型`;
            batchContextApplyBtn.disabled = selectedCount === 0;
            const pageNames = state.models.map((item) => item.model_name);
            const selectedOnPage = pageNames.filter((name) => state.selectedModelNames.has(name)).length;
            selectPageInput.checked = pageNames.length > 0 && selectedOnPage === pageNames.length;
            selectPageInput.indeterminate = selectedOnPage > 0 && selectedOnPage < pageNames.length;
        }

        function renderTable() {
            tableBody.innerHTML = state.models.map((item) => `
                <tr>
                    <td><input type="checkbox" data-model-select="${escapeHtml(item.model_name)}" aria-label="选择模型 ${escapeHtml(item.model_name)}" ${state.selectedModelNames.has(item.model_name) ? "checked" : ""}></td>
                    <td>
                        <strong>${escapeHtml(item.display_name || item.model_name)}</strong>
                        <div class="table-muted">${escapeHtml(item.model_name)}</div>
                    </td>
                    <td>${item.enabled ? '<span class="status-badge status-healthy">已启用</span>' : '<span class="status-badge status-unknown">已停用</span>'}</td>
                    <td>${renderModelAbilityCell(item)}</td>
                    <td>${renderTokenLimitCell(item)}</td>
                    <td>
                        <div>输入 ${escapeHtml(formatPrice(item.input_price_per_1k ?? item.lowest_input_price_per_1k))}</div>
                        <div class="table-muted">输出 ${escapeHtml(formatPrice(item.output_price_per_1k ?? item.lowest_output_price_per_1k))}</div>
                        <div class="table-muted">缓存 ${escapeHtml(formatPrice(item.cache_price_per_1k ?? item.lowest_cache_price_per_1k))}</div>
                    </td>
                    <td>${escapeHtml(item.speed_label || "-")}</td>
                    <td>
                        <strong>${escapeHtml(String(item.available_provider_count ?? item.enabled_provider_count ?? 0))} / ${escapeHtml(String(item.bound_provider_count ?? item.provider_count ?? 0))}</strong>
                        <div class="table-muted">${escapeHtml((item.available_provider_names || []).join("、") || "未绑定")}</div>
                    </td>
                    <td>${renderMultiplierCell(item)}</td>
                    <td>
                        <div class="table-actions">
                            <button class="table-action-btn" data-action="edit" data-model-name="${escapeHtml(item.model_name)}">编辑</button>
                            <button class="table-action-btn" data-action="delete" data-model-name="${escapeHtml(item.model_name)}">删除</button>
                        </div>
                    </td>
                </tr>
            `).join("") || '<tr><td colspan="10"><div class="empty-state">暂无模型配置</div></td></tr>';
            enhanceInteractiveButtons(tableBody);
            updateBatchBar();
        }

        function renderPagination() {
            state.totalPages = Math.max(1, Number(state.totalPages || Math.ceil((state.total || 0) / state.pageSize) || 1));
            state.page = Math.min(Math.max(1, Number(state.page || 1)), state.totalPages);
            pageMeta.textContent = `第 ${formatNumber(state.page)} 页，共 ${formatNumber(state.totalPages)} 页 · 共 ${formatNumber(state.total || 0)} 条`;
            prevPageBtn.disabled = state.page <= 1;
            nextPageBtn.disabled = state.page >= state.totalPages;
        }

        async function reloadFirstPage() {
            state.page = 1;
            await loadData({ silent: true });
        }

        function buildListParams() {
            const params = new URLSearchParams({
                paginated: "true",
                page: String(state.page),
                page_size: String(state.pageSize),
            });
            const keyword = searchInput.value.trim();
            if (keyword) params.set("keyword", keyword);
            if (enabledSelect.value) params.set("enabled", enabledSelect.value);
            if (providerSelect.value) params.set("provider_id", providerSelect.value);
            return params;
        }

        function buildBindingRows(bindings = []) {
            const rows = bindings.length ? bindings : state.providers.map((provider) => ({
                provider_id: provider.id,
                provider_name: provider.name,
                provider_enabled: provider.enabled,
                provider_health_status: provider.health_status,
                bound: false,
                enabled: true,
                priority: 100,
                weight: 100,
                price_multiplier: 1,
            }));
            bindingBody.innerHTML = rows.map((item) => `
                <tr data-provider-id="${item.provider_id}" data-initial-bound="${item.bound ? "true" : "false"}" data-bound-touched="false">
                    <td><input type="checkbox" data-binding-field="bound" ${item.bound ? "checked" : ""}></td>
                    <td>
                        <strong>${escapeHtml(item.provider_name)}</strong>
                        <div class="table-muted">${escapeHtml(formatHealthStatusLabel(item.provider_health_status))}</div>
                    </td>
                    <td>${item.provider_enabled ? "已启用" : "已停用"}</td>
                    <td><input type="checkbox" data-binding-field="enabled" ${item.enabled ? "checked" : ""}></td>
                    <td>
                        <div class="model-binding-controls">
                            <label>
                                <span>倍率</span>
                                <input class="field-input" type="number" min="0.0001" step="0.0001" data-binding-field="price_multiplier" value="${item.price_multiplier ?? 1}" title="单个渠道对当前模型的价格倍率，平均倍率会由所有已绑定渠道自动计算">
                            </label>
                            <label>
                                <span>优先级</span>
                                <input class="field-input" type="number" data-binding-field="priority" value="${item.priority ?? 100}">
                            </label>
                            <label>
                                <span>权重</span>
                                <input class="field-input" type="number" data-binding-field="weight" value="${item.weight ?? 100}">
                            </label>
                        </div>
                    </td>
                    <td data-binding-preview>-</td>
                </tr>
            `).join("");
            refreshBindingRows();
        }

        function refreshBindingRows() {
            const baseInput = inputPriceInput.value === "" ? null : Number(inputPriceInput.value);
            const baseOutput = outputPriceInput.value === "" ? null : Number(outputPriceInput.value);
            const baseCache = cachePriceInput.value === "" ? baseInput : Number(cachePriceInput.value);
            bindingBody.querySelectorAll("tr").forEach((row) => {
                const boundField = row.querySelector('input[data-binding-field="bound"]');
                const enabledField = row.querySelector('input[data-binding-field="enabled"]');
                const multiplierField = row.querySelector('input[data-binding-field="price_multiplier"]');
                const priorityField = row.querySelector('input[data-binding-field="priority"]');
                const weightField = row.querySelector('input[data-binding-field="weight"]');
                const preview = row.querySelector("[data-binding-preview]");
                const bound = boundField.checked;
                [enabledField, multiplierField, priorityField, weightField].forEach((field) => {
                    field.disabled = !bound;
                });
                if (!bound) {
                    preview.textContent = "-";
                    return;
                }
                const multiplier = Number(multiplierField.value || 1);
                const inputPreview = baseInput == null || Number.isNaN(baseInput) ? "-" : `${(baseInput * multiplier).toFixed(4)}/1M`;
                const outputPreview = baseOutput == null || Number.isNaN(baseOutput) ? "-" : `${(baseOutput * multiplier).toFixed(4)}/1M`;
                const cachePreview = baseCache == null || Number.isNaN(baseCache) ? "-" : `${(baseCache * multiplier).toFixed(4)}/1M`;
                preview.innerHTML = `<div>输入 ${escapeHtml(inputPreview)}</div><div class="table-muted">输出 ${escapeHtml(outputPreview)}</div><div class="table-muted">缓存 ${escapeHtml(cachePreview)}</div>`;
            });
        }

        function openModal(detail = null) {
            const isEditing = Boolean(detail);
            state.editingModelName = detail?.model_name || null;
            modalTitle.textContent = isEditing ? `编辑模型 ${detail.model_name}` : "新增模型";
            nameInput.value = detail?.model_name || "";
            nameInput.disabled = isEditing;
            displayNameInput.value = detail?.display_name || "";
            enabledInput.checked = detail?.enabled ?? true;
            supportsStreamInput.checked = detail?.supports_stream ?? true;
            supportsVisionInput.checked = detail?.supports_vision ?? false;
            supportsToolsInput.checked = detail?.supports_tools ?? false;
            supportsChatCompletionsInput.checked = detail?.supports_chat_completions ?? true;
            supportsResponsesInput.checked = detail?.supports_responses ?? true;
            contextWindowInput.value = detail?.context_window_tokens == null ? "" : detail.context_window_tokens;
            maxInputTokensInput.value = detail?.max_input_tokens == null ? "" : detail.max_input_tokens;
            maxOutputTokensInput.value = detail?.max_output_tokens == null ? "" : detail.max_output_tokens;
            inputPriceInput.value = detail?.input_price_per_1k == null ? "" : toPricePer1M(detail.input_price_per_1k);
            outputPriceInput.value = detail?.output_price_per_1k == null ? "" : toPricePer1M(detail.output_price_per_1k);
            cachePriceInput.value = detail?.cache_price_per_1k == null
                ? (detail?.input_price_per_1k == null ? "" : toPricePer1M(detail.input_price_per_1k))
                : toPricePer1M(detail.cache_price_per_1k);
            speedLabelInput.value = detail?.speed_label || "";
            remarkInput.value = detail?.remark || "";
            buildBindingRows(detail?.provider_bindings || []);
            modal.classList.remove("hidden");
        }

        function closeModal() {
            modal.classList.add("hidden");
            form.reset();
            bindingBody.innerHTML = "";
            state.editingModelName = null;
            nameInput.disabled = false;
        }

        function collectBindings({ isEditing = false } = {}) {
            return Array.from(bindingBody.querySelectorAll("tr")).map((row) => {
                const initialBound = row.dataset.initialBound === "true";
                const boundTouched = row.dataset.boundTouched === "true";
                const requestedBound = row.querySelector('input[data-binding-field="bound"]').checked;
                const effectiveBound = isEditing && !initialBound && requestedBound && !boundTouched ? false : requestedBound;
                return {
                    provider_id: Number(row.dataset.providerId),
                    bound: effectiveBound,
                    enabled: row.querySelector('input[data-binding-field="enabled"]').checked,
                    price_multiplier: Number(row.querySelector('input[data-binding-field="price_multiplier"]').value || 1),
                    priority: Number(row.querySelector('input[data-binding-field="priority"]').value || 100),
                    weight: Number(row.querySelector('input[data-binding-field="weight"]').value || 100),
                };
            });
        }

        async function loadData({ silent = false, reloadProviders = false } = {}) {
            const providerPromise = reloadProviders || !state.providers.length
                ? api.get("/api/providers")
                : Promise.resolve(state.providers);
            const [result, providers] = await Promise.all([
                api.get(`/api/models?${buildListParams().toString()}`),
                providerPromise,
            ]);
            state.providers = providers;
            renderProviderFilterOptions();
            state.models = Array.isArray(result.items) ? result.items : [];
            state.total = Number(result.total || 0);
            state.page = Number(result.page || state.page || 1);
            state.pageSize = Number(result.page_size || state.pageSize || 20);
            state.totalPages = Number(result.total_pages || 1);
            pageSizeSelect.value = String(state.pageSize);
            updateSummary(result.summary || {});
            renderTable();
            renderPagination();
            if (!silent) showToast("模型配置已刷新");
        }

        tableBody.addEventListener("click", async (event) => {
            const button = event.target.closest("[data-action]");
            if (!button) return;
            if (button.dataset.action === "delete") {
                if (!window.confirm(`确认删除模型 ${button.dataset.modelName} 吗？相关渠道绑定也会一并移除。`)) {
                    return;
                }
                try {
                    await api.delete(`/api/models/${encodeURIComponent(button.dataset.modelName)}`);
                    showToast("模型已删除");
                    await loadData({ silent: true });
                } catch (error) {
                    showToast(error.message, "error");
                }
                return;
            }
            try {
                const detail = await api.get(`/api/models/${encodeURIComponent(button.dataset.modelName)}`);
                openModal(detail);
            } catch (error) {
                showToast(error.message, "error");
            }
        });

        tableBody.addEventListener("change", (event) => {
            const checkbox = event.target.closest("[data-model-select]");
            if (!checkbox) return;
            const modelName = checkbox.dataset.modelSelect;
            if (!modelName) return;
            if (checkbox.checked) {
                state.selectedModelNames.add(modelName);
            } else {
                state.selectedModelNames.delete(modelName);
            }
            updateBatchBar();
        });

        selectPageInput.addEventListener("change", () => {
            state.models.forEach((item) => {
                if (selectPageInput.checked) {
                    state.selectedModelNames.add(item.model_name);
                } else {
                    state.selectedModelNames.delete(item.model_name);
                }
            });
            renderTable();
        });

        batchContextApplyBtn.addEventListener("click", async () => {
            const modelNames = Array.from(state.selectedModelNames);
            if (!modelNames.length) {
                showToast("请先选择模型", "error");
                return;
            }
            if (batchContextWindowInput.value === "") {
                showToast("请填写要应用的上下文窗口 token", "error");
                return;
            }
            const contextWindowTokens = Number(batchContextWindowInput.value);
            if (!Number.isFinite(contextWindowTokens) || contextWindowTokens < 1) {
                showToast("上下文窗口 token 必须大于 0", "error");
                return;
            }
            try {
                setButtonLoading(batchContextApplyBtn, true);
                await api.post("/api/models/batch/context-window", {
                    model_names: modelNames,
                    context_window_tokens: Math.floor(contextWindowTokens),
                });
                showToast("上下文窗口已批量应用");
                await loadData({ silent: true });
            } catch (error) {
                showToast(error.message, "error");
            } finally {
                setButtonLoading(batchContextApplyBtn, false);
            }
        });

        bindingBody.addEventListener("input", refreshBindingRows);
        bindingBody.addEventListener("change", (event) => {
            if (event.target.matches('input[data-binding-field="bound"]')) {
                const row = event.target.closest("tr");
                if (row) {
                    row.dataset.boundTouched = "true";
                }
            }
            refreshBindingRows();
        });
        inputPriceInput.addEventListener("input", refreshBindingRows);
        outputPriceInput.addEventListener("input", refreshBindingRows);
        cachePriceInput.addEventListener("input", refreshBindingRows);

        form.addEventListener("submit", async (event) => {
            event.preventDefault();
            const payload = {
                display_name: displayNameInput.value.trim() || null,
                enabled: enabledInput.checked,
                supports_stream: supportsStreamInput.checked,
                supports_vision: supportsVisionInput.checked,
                supports_tools: supportsToolsInput.checked,
                supports_chat_completions: supportsChatCompletionsInput.checked,
                supports_responses: supportsResponsesInput.checked,
                context_window_tokens: contextWindowInput.value === "" ? null : Number(contextWindowInput.value),
                max_input_tokens: maxInputTokensInput.value === "" ? null : Number(maxInputTokensInput.value),
                max_output_tokens: maxOutputTokensInput.value === "" ? null : Number(maxOutputTokensInput.value),
                input_price_per_1k: inputPriceInput.value === "" ? null : toPricePer1K(Number(inputPriceInput.value)),
                output_price_per_1k: outputPriceInput.value === "" ? null : toPricePer1K(Number(outputPriceInput.value)),
                cache_price_per_1k: cachePriceInput.value === ""
                    ? (inputPriceInput.value === "" ? null : toPricePer1K(Number(inputPriceInput.value)))
                    : toPricePer1K(Number(cachePriceInput.value)),
                speed_label: speedLabelInput.value.trim() || null,
                remark: remarkInput.value.trim() || null,
                provider_bindings: collectBindings({ isEditing: Boolean(state.editingModelName) }),
            };
            if (!state.editingModelName) {
                payload.model_name = nameInput.value.trim();
                if (!payload.model_name) {
                    showToast("请填写模型名", "error");
                    return;
                }
            }
            try {
                setButtonLoading(submitBtn, true);
                if (state.editingModelName) {
                    await api.put(`/api/models/${encodeURIComponent(state.editingModelName)}`, payload);
                    showToast("模型配置已更新");
                } else {
                    await api.post("/api/models", payload);
                    showToast("模型已创建");
                }
                closeModal();
                await loadData({ silent: true, reloadProviders: true });
            } catch (error) {
                showToast(error.message, "error");
            } finally {
                setButtonLoading(submitBtn, false);
            }
        });

        addBtn.addEventListener("click", () => openModal());
        refreshBtn.addEventListener("click", async () => {
            try {
                setButtonLoading(refreshBtn, true);
                await loadData({ reloadProviders: true });
            } catch (error) {
                showToast(error.message, "error");
            } finally {
                setButtonLoading(refreshBtn, false);
            }
        });
        searchInput.addEventListener("input", () => {
            window.clearTimeout(searchTimer);
            searchTimer = window.setTimeout(async () => {
                try {
                    await reloadFirstPage();
                } catch (error) {
                    showToast(error.message, "error");
                }
            }, 250);
        });
        enabledSelect.addEventListener("change", async () => {
            try {
                await reloadFirstPage();
            } catch (error) {
                showToast(error.message, "error");
            }
        });
        providerSelect.addEventListener("change", async () => {
            try {
                await reloadFirstPage();
            } catch (error) {
                showToast(error.message, "error");
            }
        });
        pageSizeSelect.addEventListener("change", async () => {
            state.pageSize = Number.parseInt(pageSizeSelect.value || "20", 10) || 20;
            try {
                await reloadFirstPage();
            } catch (error) {
                showToast(error.message, "error");
            }
        });
        prevPageBtn.addEventListener("click", async () => {
            if (state.page <= 1) return;
            state.page -= 1;
            try {
                await loadData({ silent: true });
            } catch (error) {
                showToast(error.message, "error");
            }
        });
        nextPageBtn.addEventListener("click", async () => {
            if (state.page >= state.totalPages) return;
            state.page += 1;
            try {
                await loadData({ silent: true });
            } catch (error) {
                showToast(error.message, "error");
            }
        });
        closeBtn.addEventListener("click", closeModal);
        cancelBtn.addEventListener("click", closeModal);
        modal.addEventListener("click", (event) => {
            if (event.target === modal) closeModal();
        });

        await loadData({ silent: true, reloadProviders: true });
    }

    async function initSettings() {
        const form = document.getElementById("settings-form");
        const submitBtn = document.getElementById("settings-submit-btn");
        const providerSelect = document.getElementById("setting-default-provider-id");
        const routeModeSelect = document.getElementById("setting-route-mode");
        const manualAllowFallbackInput = document.getElementById("setting-manual-allow-fallback");
        const healthCheckIntervalInput = document.getElementById("setting-health-check-interval-sec");
        const [providers, settings] = await Promise.all([api.get("/api/providers"), api.get("/api/settings")]);

        providerSelect.innerHTML = '<option value="">未设置</option>' + providers.map((provider) => `
            <option value="${provider.id}">${escapeHtml(provider.name)}</option>
        `).join("");

        routeModeSelect.value = settings.route_mode;
        document.getElementById("setting-default-provider-id").value = settings.default_provider_id ?? "";
        document.getElementById("setting-global-timeout-ms").value = settings.global_timeout_ms;
        document.getElementById("setting-global-max-retries").value = settings.global_max_retries;
        document.getElementById("setting-global-max-request-tokens").value = settings.global_max_request_tokens ?? 0;
        document.getElementById("setting-max-v1-request-body-bytes").value = settings.max_v1_request_body_bytes ?? 20971520;
        document.getElementById("setting-max-v1-chat-request-body-bytes").value = settings.max_v1_chat_request_body_bytes ?? 0;
        document.getElementById("setting-max-v1-responses-request-body-bytes").value = settings.max_v1_responses_request_body_bytes ?? 0;
        document.getElementById("setting-long-output-stream-threshold-tokens").value = settings.long_output_stream_threshold_tokens ?? 8192;
        document.getElementById("setting-max-non-stream-response-body-bytes").value = settings.max_non_stream_response_body_bytes ?? 20971520;
        document.getElementById("setting-stream-token-capture-max-bytes").value = settings.stream_token_capture_max_bytes ?? 1048576;
        document.getElementById("setting-max-logged-metadata-bytes").value = settings.max_logged_metadata_bytes ?? 1024;
        document.getElementById("setting-circuit-breaker-threshold").value = settings.circuit_breaker_threshold;
        healthCheckIntervalInput.value = settings.health_check_interval_sec;
        healthCheckIntervalInput.min = "300";
        document.getElementById("setting-recovery-probe-interval-sec").value = settings.recovery_probe_interval_sec;
        document.getElementById("setting-max-logged-body-bytes").value = settings.max_logged_body_bytes;
        manualAllowFallbackInput.checked = settings.manual_allow_fallback;
        document.getElementById("setting-auto-health-check").checked = settings.auto_health_check;
        document.getElementById("setting-enable-token-logging").checked = settings.enable_token_logging;
        document.getElementById("setting-enable-payload-logging").checked = settings.enable_payload_logging;
        document.getElementById("setting-enable-stream-response-persist").checked = settings.enable_stream_response_persist;
        document.getElementById("setting-mask-sensitive-fields").checked = settings.mask_sensitive_fields;
        document.getElementById("setting-allow-public-user-registration").checked = settings.allow_public_user_registration;
        document.getElementById("setting-request-log-retention-days").value = settings.request_log_retention_days;
        document.getElementById("setting-admin-audit-log-retention-days").value = settings.admin_audit_log_retention_days;
        document.getElementById("setting-global-max-active-requests").value = settings.global_max_active_requests;
        document.getElementById("setting-global-max-active-streams").value = settings.global_max_active_streams;
        document.getElementById("setting-api-key-max-active-requests").value = settings.api_key_max_active_requests;
        document.getElementById("setting-api-key-max-active-streams").value = settings.api_key_max_active_streams;
        document.getElementById("setting-account-max-active-requests").value = settings.account_max_active_requests;
        document.getElementById("setting-account-max-active-streams").value = settings.account_max_active_streams;
        document.getElementById("setting-provider-max-active-requests").value = settings.provider_max_active_requests;
        document.getElementById("setting-provider-max-active-streams").value = settings.provider_max_active_streams;
        document.getElementById("setting-concurrency-lease-ttl-seconds").value = settings.concurrency_lease_ttl_seconds;

        const refreshRouteGuide = () => {
            renderRoutePolicyGuide({
                routeMode: routeModeSelect.value,
                defaultProviderId: providerSelect.value ? Number(providerSelect.value) : null,
                manualAllowFallback: manualAllowFallbackInput.checked,
                providers,
            });
        };

        refreshRouteGuide();
        routeModeSelect.addEventListener("change", refreshRouteGuide);
        providerSelect.addEventListener("change", refreshRouteGuide);
        manualAllowFallbackInput.addEventListener("change", refreshRouteGuide);

        form.addEventListener("submit", async (event) => {
            event.preventDefault();
            const payload = {
                route_mode: routeModeSelect.value,
                default_provider_id: providerSelect.value ? Number(providerSelect.value) : null,
                manual_allow_fallback: manualAllowFallbackInput.checked,
                global_timeout_ms: Number(document.getElementById("setting-global-timeout-ms").value),
                global_max_retries: Number(document.getElementById("setting-global-max-retries").value),
                global_max_request_tokens: Number(document.getElementById("setting-global-max-request-tokens").value),
                max_v1_request_body_bytes: Number(document.getElementById("setting-max-v1-request-body-bytes").value),
                max_v1_chat_request_body_bytes: Number(document.getElementById("setting-max-v1-chat-request-body-bytes").value),
                max_v1_responses_request_body_bytes: Number(document.getElementById("setting-max-v1-responses-request-body-bytes").value),
                long_output_stream_threshold_tokens: Number(document.getElementById("setting-long-output-stream-threshold-tokens").value),
                max_non_stream_response_body_bytes: Number(document.getElementById("setting-max-non-stream-response-body-bytes").value),
                stream_token_capture_max_bytes: Number(document.getElementById("setting-stream-token-capture-max-bytes").value),
                max_logged_metadata_bytes: Number(document.getElementById("setting-max-logged-metadata-bytes").value),
                circuit_breaker_threshold: Number(document.getElementById("setting-circuit-breaker-threshold").value),
                auto_health_check: document.getElementById("setting-auto-health-check").checked,
                health_check_interval_sec: Math.max(300, Number(healthCheckIntervalInput.value || 300)),
                recovery_probe_interval_sec: Number(document.getElementById("setting-recovery-probe-interval-sec").value),
                enable_token_logging: document.getElementById("setting-enable-token-logging").checked,
                enable_payload_logging: document.getElementById("setting-enable-payload-logging").checked,
                enable_stream_response_persist: document.getElementById("setting-enable-stream-response-persist").checked,
                mask_sensitive_fields: document.getElementById("setting-mask-sensitive-fields").checked,
                max_logged_body_bytes: Number(document.getElementById("setting-max-logged-body-bytes").value),
                allow_public_user_registration: document.getElementById("setting-allow-public-user-registration").checked,
                request_log_retention_days: Number(document.getElementById("setting-request-log-retention-days").value),
                admin_audit_log_retention_days: Number(document.getElementById("setting-admin-audit-log-retention-days").value),
                global_max_active_requests: Number(document.getElementById("setting-global-max-active-requests").value),
                global_max_active_streams: Number(document.getElementById("setting-global-max-active-streams").value),
                api_key_max_active_requests: Number(document.getElementById("setting-api-key-max-active-requests").value),
                api_key_max_active_streams: Number(document.getElementById("setting-api-key-max-active-streams").value),
                account_max_active_requests: Number(document.getElementById("setting-account-max-active-requests").value),
                account_max_active_streams: Number(document.getElementById("setting-account-max-active-streams").value),
                provider_max_active_requests: Number(document.getElementById("setting-provider-max-active-requests").value),
                provider_max_active_streams: Number(document.getElementById("setting-provider-max-active-streams").value),
                concurrency_lease_ttl_seconds: Number(document.getElementById("setting-concurrency-lease-ttl-seconds").value),
            };
            try {
                setButtonLoading(submitBtn, true);
                await api.put("/api/settings", payload);
                showToast("设置已保存");
            } catch (error) {
                showToast(error.message, "error");
            } finally {
                setButtonLoading(submitBtn, false);
            }
        });
    }

    const SUPPORTED_IMAGE_UPLOAD_TYPES = new Set(["image/png", "image/jpeg", "image/webp", "image/gif"]);
    const MAX_IMAGE_UPLOAD_BYTES = 10 * 1024 * 1024;

    function describeImageDetailMode(detail) {
        if (detail === "low") {
            return "低细节会优先压低图片理解成本，适合只看大轮廓或主物体。";
        }
        if (detail === "high") {
            return "高细节会读取更多图像细节，更适合识别小字、截图和密集内容，但请求成本更高。";
        }
        return "自动细节会交给上游模型自行决定，是默认也最稳妥的图片请求模式。";
    }

    function getImageUploadConstraintText() {
        return "支持 PNG/JPEG/WEBP/GIF，单图最大 10 MB。";
    }

    function validateImageUploadFile(file) {
        if (!file) {
            return "请先选择一张本地图片";
        }
        const contentType = String(file.type || "").toLowerCase();
        if (!SUPPORTED_IMAGE_UPLOAD_TYPES.has(contentType)) {
            return "仅支持 PNG/JPEG/WEBP/GIF 图片";
        }
        if (Number(file.size || 0) <= 0) {
            return "上传文件不能为空";
        }
        if (Number(file.size || 0) > MAX_IMAGE_UPLOAD_BYTES) {
            return "图片大小不能超过 10 MB";
        }
        return null;
    }

    async function initPlayground() {
        const form = document.getElementById("playground-form");
        const clearBtn = document.getElementById("playground-clear");
        const submitBtn = document.getElementById("playground-submit-btn");
        const meta = document.getElementById("playground-meta");
        const output = document.getElementById("playground-output");
        const endpointSelect = document.getElementById("playground-endpoint");
        const providerSelect = document.getElementById("playground-provider");
        const modelSelect = document.getElementById("playground-model");
        const imageModeSelect = document.getElementById("playground-image-mode");
        const imageDetailSelect = document.getElementById("playground-image-detail");
        const imageUrlField = document.getElementById("playground-image-url-field");
        const imageUrlInput = document.getElementById("playground-image-url");
        const imageFileField = document.getElementById("playground-image-file-field");
        const imageFileInput = document.getElementById("playground-image-file");
        const imageNote = document.getElementById("playground-image-note");
        const batchForm = document.getElementById("playground-batch-form");
        const batchMeta = document.getElementById("playground-batch-meta");
        const batchOutput = document.getElementById("playground-batch-output");
        const batchSubmitBtn = document.getElementById("playground-batch-submit-btn");
        const batchSelectEnabledBtn = document.getElementById("playground-batch-select-enabled");
        const batchSelectAllBtn = document.getElementById("playground-batch-select-all");
        const batchClearBtn = document.getElementById("playground-batch-clear");
        let providerOptions = [];
        let modelCatalogOptions = [];
        const batchResultsState = {
            results: [],
            keyword: "",
            health: "",
            page: 1,
            pageSize: 10,
        };

        function renderBatchResultsView() {
            if (!batchResultsState.results.length) {
                setBatchPlaceholder("等待批量测试...");
                return;
            }
            showBatchRendered(renderBatchConnectivityResults(batchResultsState.results, batchResultsState));
            enhanceInteractiveButtons(batchOutput);
        }

        function updatePlaygroundImageFields() {
            const mode = imageModeSelect.value || "none";
            const detailText = describeImageDetailMode(imageDetailSelect.value || "auto");
            const constraintText = getImageUploadConstraintText();
            imageUrlField.classList.toggle("hidden", mode !== "url");
            imageFileField.classList.toggle("hidden", mode !== "upload");
            if (mode === "url") {
                imageNote.textContent = `将按标准图片链接请求发送，适合公网可访问图片地址。${detailText} ${constraintText}`;
                return;
            }
            if (mode === "upload") {
                const file = imageFileInput.files?.[0];
                const fileError = file ? validateImageUploadFile(file) : null;
                imageNote.textContent = file
                    ? `${isLocalBrowserHost()
                        ? `当前为本地访问，将以 data URL 形式直接发送本地图片：${file.name}。`
                        : `将先上传到当前服务，再把可访问图片地址发送给模型：${file.name}。`} ${fileError || detailText} ${constraintText}`
                    : (isLocalBrowserHost()
                        ? `将把本地图片转换为 data URL 后发往内部接口，适合直接测试视觉模型。${detailText} ${constraintText}`
                        : `将先把本地图片上传到当前服务，再把生成的图片地址发给模型。${detailText} ${constraintText}`);
                return;
            }
            imageNote.textContent = `当前仅发送文本。若切换为图片链接或本地上传，playground 会自动按所选内部接口拼装视觉请求。${detailText} ${constraintText}`;
        }

        function buildChatMessageContent(messageText, imageInput) {
            const content = [];
            if (messageText) {
                content.push({ type: "text", text: messageText });
            }
            if (imageInput) {
                content.push({
                    type: "image_url",
                    image_url: {
                        url: imageInput.url,
                        detail: imageInput.detail,
                    },
                });
            }
            return content;
        }

        function buildResponsesInputContent(messageText, imageInput) {
            const content = [];
            if (messageText) {
                content.push({ type: "input_text", text: messageText });
            }
            if (imageInput) {
                content.push({
                    type: "input_image",
                    image_url: imageInput.url,
                    detail: imageInput.detail,
                });
            }
            return content;
        }

        async function readImageFileAsDataUrl(file) {
            return await new Promise((resolve, reject) => {
                const reader = new FileReader();
                reader.onload = () => {
                    if (typeof reader.result === "string" && reader.result) {
                        resolve(reader.result);
                        return;
                    }
                    reject(new Error("图片读取失败"));
                };
                reader.onerror = () => reject(new Error("图片读取失败"));
                reader.readAsDataURL(file);
            });
        }

        function isLocalBrowserHost() {
            const hostname = window.location.hostname || "";
            return hostname === "localhost" || hostname === "127.0.0.1" || hostname === "::1";
        }

        async function uploadPlaygroundImage(file) {
            const formData = new FormData();
            formData.append("file", file);
            const response = await fetch("/api/playground/assets/upload", {
                method: "POST",
                body: formData,
                credentials: "same-origin",
            });
            if (!response.ok) {
                const text = await response.text();
                const data = safeJsonParse(text) ?? text;
                throw new Error(typeof data === "string" ? data : JSON.stringify(data, null, 2));
            }
            return await response.json();
        }

        async function resolvePlaygroundImageInput() {
            const mode = imageModeSelect.value || "none";
            if (mode === "none") {
                return null;
            }
            const detail = imageDetailSelect.value || "auto";
            if (mode === "url") {
                const url = imageUrlInput.value.trim();
                if (!url) {
                    throw new Error("请选择图片链接，或切换为不附带图片");
                }
                return { url, detail };
            }
            const file = imageFileInput.files?.[0];
            const fileError = validateImageUploadFile(file);
            if (fileError) {
                throw new Error(fileError);
            }
            if (isLocalBrowserHost()) {
                return {
                    url: await readImageFileAsDataUrl(file),
                    detail,
                    source: "data-url",
                };
            }
            const uploaded = await uploadPlaygroundImage(file);
            return {
                url: uploaded.asset_url,
                detail,
                source: "uploaded-asset",
                assetId: uploaded.id,
                assetUrl: uploaded.asset_url,
            };
        }

        function renderPlaygroundProviders(providers) {
            providerSelect.innerHTML = '<option value="">自动路由（按当前规则）</option>' + providers
                .filter((provider) => provider.enabled)
                .map((provider) => `<option value="${provider.id}">${escapeHtml(provider.name)}</option>`)
                .join("");
        }

        function renderPlaygroundModels() {
            const selectedProviderId = providerSelect.value ? Number(providerSelect.value) : null;
            const selectedProvider = selectedProviderId
                ? providerOptions.find((provider) => provider.id === selectedProviderId)
                : null;
            const requireStream = document.getElementById("playground-stream").checked;
            const requireVision = (imageModeSelect.value || "none") !== "none";
            const enabledModelNameSet = new Set(
                modelCatalogOptions
                    .filter((model) => model.enabled && (model.enabled_provider_count || 0) > 0)
                    .map((model) => model.model_name)
            );
            const models = selectedProvider
                ? collectProviderConfiguredModels(selectedProvider, { requireStream, requireVision, allowedModelNameSet: enabledModelNameSet })
                : collectConfiguredModels(providerOptions, { requireStream, requireVision, allowedModelNameSet: enabledModelNameSet });
            const previousModel = modelSelect.value;
            modelSelect.innerHTML = '<option value="">请先选择一个已配置模型</option>' + models.map((modelName) => (
                `<option value="${escapeHtml(modelName)}">${escapeHtml(modelName)}</option>`
            )).join("");
            if (previousModel && models.includes(previousModel)) {
                modelSelect.value = previousModel;
            } else if (models.length === 1) {
                modelSelect.value = models[0];
            }
            if (!models.length) {
                const requirementLabels = [];
                if (requireVision) requirementLabels.push("图片");
                if (requireStream) requirementLabels.push("stream");
                const requirementText = requirementLabels.length ? `（需支持 ${requirementLabels.join(" + ")}）` : "";
                const message = selectedProvider
                    ? `中转站 ${selectedProvider.name} 当前没有可用于测试的已启用模型${requirementText}`
                    : `当前没有可用于测试的已启用模型${requirementText}，请先到模型配置中启用模型并绑定可用中转站`;
                showToast(message, "error");
            }
        }

        async function loadPlaygroundModels() {
            [providerOptions, modelCatalogOptions] = await Promise.all([
                api.get("/api/providers"),
                api.get("/api/models"),
            ]);
            renderPlaygroundProviders(providerOptions);
            renderPlaygroundModels();
            renderBatchProviderPicker(providerOptions);
        }

        clearBtn.addEventListener("click", () => {
            providerSelect.value = "";
            renderPlaygroundModels();
            modelSelect.value = modelSelect.options.length > 1 ? modelSelect.options[1].value : "";
            endpointSelect.value = "chat-completions";
            document.getElementById("playground-message").value = "";
            document.getElementById("playground-stream").checked = false;
            imageModeSelect.value = "none";
            imageDetailSelect.value = "auto";
            imageUrlInput.value = "";
            imageFileInput.value = "";
            updatePlaygroundImageFields();
            meta.innerHTML = "";
            setPlaygroundPlaceholder("等待请求...");
            setButtonTransientFeedback(clearBtn, "success", { successText: "已清空" });
        });

        providerSelect.addEventListener("change", () => {
            renderPlaygroundModels();
        });
        imageModeSelect.addEventListener("change", () => {
            updatePlaygroundImageFields();
            renderPlaygroundModels();
        });
        imageDetailSelect.addEventListener("change", updatePlaygroundImageFields);
        imageFileInput.addEventListener("change", updatePlaygroundImageFields);
        document.getElementById("playground-stream").addEventListener("change", () => {
            renderPlaygroundModels();
        });

        form.addEventListener("submit", async (event) => {
            event.preventDefault();
            if (!modelSelect.value) {
                showToast("请先选择一个已配置模型", "error");
                return;
            }
            const payload = {
                model: modelSelect.value,
                stream: document.getElementById("playground-stream").checked,
            };
            const endpointValue = endpointSelect.value === "responses" ? "responses" : "chat-completions";
            const endpointLabel = endpointValue === "responses" ? "responses" : "chat/completions";
            try {
                const messageValue = document.getElementById("playground-message").value.trim();
                const imageInput = await resolvePlaygroundImageInput();
                if (!messageValue && !imageInput) {
                    showToast("请至少填写文本或附带一张图片", "error");
                    return;
                }
                if (endpointValue === "responses") {
                    payload.input = imageInput
                        ? [{ role: "user", content: buildResponsesInputContent(messageValue, imageInput) }]
                        : messageValue;
                } else {
                    payload.messages = [{
                        role: "user",
                        content: imageInput ? buildChatMessageContent(messageValue, imageInput) : messageValue,
                    }];
                }
                payload.endpointLabel = endpointLabel;
                const selectedProviderId = providerSelect.value ? Number(providerSelect.value) : null;
                const requestHeaders = selectedProviderId ? { "X-Aotu-Provider-Id": String(selectedProviderId) } : {};
                meta.textContent = "请求发送中...";
                setPlaygroundPlaceholder("请求发送中...");
                setButtonLoading(submitBtn, true);
                const requestPayload = { ...payload };
                delete requestPayload.endpointLabel;
                const endpointUrl = endpointValue === "responses"
                    ? "/api/playground/responses"
                    : "/api/playground/chat-completions";
                const response = await fetch(endpointUrl, withJson("POST", requestPayload, requestHeaders));
                if (!response.ok) {
                    const text = await response.text();
                    const data = safeJsonParse(text) ?? text;
                    throw new Error(typeof data === "string" ? data : JSON.stringify(data, null, 2));
                }
                if (payload.stream) {
                    await readStreamingResponse(response, payload, meta);
                } else {
                    const text = await response.text();
                    const data = safeJsonParse(text) ?? text;
                    meta.innerHTML = "";
                    showPlaygroundRendered(renderPlaygroundResponse(data, {
                        isStream: false,
                        model: payload.model,
                        providerName: readProxyProviderName(response.headers),
                        latencyMs: response.headers.get("X-Proxy-Latency-Ms") || "-",
                    }));
                }
                showToast("请求成功");
            } catch (error) {
                meta.innerHTML = "";
                showPlaygroundRendered(`
                    <section class="playground-result-card playground-result-error">
                        <div class="playground-card-title">请求失败</div>
                        <div class="playground-reply-content">${renderReplyContent(error.message)}</div>
                    </section>
                `);
                showToast("请求失败", "error");
            } finally {
                setButtonLoading(submitBtn, false);
            }
        });

        updatePlaygroundImageFields();

        batchSelectEnabledBtn.addEventListener("click", () => updateBatchSelection(providerOptions, "enabled"));
        batchSelectAllBtn.addEventListener("click", () => updateBatchSelection(providerOptions, "all"));
        batchClearBtn.addEventListener("click", () => updateBatchSelection(providerOptions, "none"));

        batchOutput?.addEventListener("input", (event) => {
            const target = event.target;
            if (!(target instanceof HTMLInputElement)) return;
            if (target.id === "playground-batch-search") {
                batchResultsState.keyword = target.value || "";
                batchResultsState.page = 1;
                renderBatchResultsView();
            }
        });

        batchOutput?.addEventListener("change", (event) => {
            const target = event.target;
            if (!(target instanceof HTMLSelectElement)) return;
            if (target.id === "playground-batch-health") {
                batchResultsState.health = target.value || "";
                batchResultsState.page = 1;
                renderBatchResultsView();
                return;
            }
            if (target.id === "playground-batch-page-size") {
                batchResultsState.pageSize = Number(target.value) || 10;
                batchResultsState.page = 1;
                renderBatchResultsView();
            }
        });

        batchOutput?.addEventListener("click", (event) => {
            if (!(event.target instanceof Element)) return;
            const button = event.target.closest("button");
            if (!button) return;
            if (button.id === "playground-batch-prev-page" && batchResultsState.page > 1) {
                batchResultsState.page -= 1;
                renderBatchResultsView();
                return;
            }
            if (button.id === "playground-batch-next-page") {
                batchResultsState.page += 1;
                renderBatchResultsView();
            }
        });

        batchForm.addEventListener("submit", async (event) => {
            event.preventDefault();
            const providerIds = getSelectedProviderIds();
            if (!providerIds.length) {
                showToast("请至少选择一个渠道", "error");
                return;
            }
            batchMeta.textContent = `批量测试中，共 ${providerIds.length} 个渠道...`;
            setBatchPlaceholder("批量测试中...");
            try {
                setButtonLoading(batchSubmitBtn, true);
                const results = await api.post("/api/providers/test-connectivity", { provider_ids: providerIds });
                batchResultsState.results = Array.isArray(results) ? results : [];
                batchResultsState.keyword = "";
                batchResultsState.health = "";
                batchResultsState.page = 1;
                batchMeta.textContent = `批量测试完成，共 ${batchResultsState.results.length} 个渠道`;
                renderBatchResultsView();
                showToast("批量测试完成");
            } catch (error) {
                batchResultsState.results = [];
                batchMeta.textContent = "";
                showBatchRendered(`
                    <section class="playground-result-card playground-result-error">
                        <div class="playground-card-title">批量测试失败</div>
                        <div class="playground-reply-content">${renderReplyContent(error.message)}</div>
                    </section>
                `);
                showToast("批量测试失败", "error");
            } finally {
                setButtonLoading(batchSubmitBtn, false);
            }
        });

        await loadPlaygroundModels();
    }

    async function initUserModels() {
        const form = document.getElementById("user-asset-upload-form");
        const fileInput = document.getElementById("user-asset-file");
        const submitBtn = document.getElementById("user-asset-upload-btn");
        const meta = document.getElementById("user-asset-upload-meta");
        const result = document.getElementById("user-asset-upload-result");
        if (!form || !fileInput || !submitBtn || !meta || !result) {
            return;
        }

        form.addEventListener("submit", async (event) => {
            event.preventDefault();
            const file = fileInput.files?.[0];
            if (!file) {
                showToast("请先选择一张图片", "error");
                return;
            }
            const fileError = validateImageUploadFile(file);
            if (fileError) {
                showToast(fileError, "error");
                return;
            }
            const formData = new FormData();
            formData.append("file", file);
            try {
                setButtonLoading(submitBtn, true);
                meta.textContent = "图片上传中...";
                const response = await fetch("/api/user/assets/upload", {
                    method: "POST",
                    body: formData,
                    credentials: "same-origin",
                });
                if (!response.ok) {
                    const text = await response.text();
                    const data = safeJsonParse(text) ?? text;
                    throw new Error(typeof data === "string" ? data : JSON.stringify(data, null, 2));
                }
                const data = await response.json();
                meta.textContent = "图片上传完成，可直接复制地址用于多模态请求。";
                result.innerHTML = `
                    <div><span>文件名</span><strong>${escapeHtml(data.filename || "-")}</strong></div>
                    <div><span>大小</span><strong>${formatNumber(data.file_size_bytes || 0)} B</strong></div>
                    <div><span>图片地址</span><strong><button class="btn btn-ghost btn-sm interactive-btn" type="button" data-copy-text="${escapeHtml(data.asset_url)}">复制地址</button></strong></div>
                    <div><span>访问路径</span><strong>${escapeHtml(data.public_path || "-")}</strong></div>
                    <div><span>调用示例</span><strong><button class="btn btn-ghost btn-sm interactive-btn" type="button" data-copy-text="${escapeHtml(JSON.stringify({ type: "image_url", image_url: { url: data.asset_url, detail: "auto" } }))}">复制消息片段</button></strong></div>
                `;
                enhanceInteractiveButtons(result);
                showToast("图片上传成功");
            } catch (error) {
                meta.textContent = "图片上传失败";
                result.innerHTML = `<div><span>错误</span><strong>${escapeHtml(error.message)}</strong></div>`;
                showToast("图片上传失败", "error");
            } finally {
                setButtonLoading(submitBtn, false);
            }
        });
    }

    async function initUserApiKeys() {
        const cards = Array.from(document.querySelectorAll("[data-user-key-card]"));
        const directory = document.getElementById("user-api-key-directory");
        const searchInput = document.getElementById("user-api-key-search");
        const filterButtons = Array.from(document.querySelectorAll("[data-user-key-filter]"));
        const visibleCountNode = document.getElementById("user-api-key-visible-count");
        const emptyNode = document.getElementById("user-api-key-empty-filtered");
        const createPanel = document.getElementById("user-api-key-create-panel");
        const state = {
            query: "",
            filter: "all",
        };

        if (!searchInput || !filterButtons.length) {
            return;
        }

        function matchesFilter(card) {
            const text = String(card.dataset.filterText || "");
            const status = String(card.dataset.status || "");
            const hasRaw = card.dataset.hasRaw === "true";
            const queryMatched = !state.query || text.includes(state.query);
            if (!queryMatched) return false;
            if (state.filter === "all") return true;
            if (state.filter === "active") return status === "active";
            if (state.filter === "disabled") return status === "disabled";
            if (state.filter === "missing-raw") return !hasRaw;
            if (state.filter === "risk") return status !== "active" && status !== "disabled";
            return true;
        }

        function syncCardState(card) {
            const editor = card.querySelector("[data-user-key-editor]");
            card.classList.toggle("is-editing", Boolean(editor?.open));
        }

        function closeOtherEditors(activeCard = null) {
            cards.forEach((card) => {
                if (card === activeCard) return;
                const editor = card.querySelector("[data-user-key-editor]");
                if (editor?.open) {
                    editor.open = false;
                }
            });
        }

        function applyFilters() {
            let visibleCount = 0;
            cards.forEach((card) => {
                const visible = matchesFilter(card);
                card.classList.toggle("hidden", !visible);
                if (visible) visibleCount += 1;
            });
            if (visibleCountNode) {
                visibleCountNode.textContent = formatNumber(visibleCount);
            }
            if (emptyNode) {
                emptyNode.classList.toggle("hidden", visibleCount !== 0);
            }
        }

        filterButtons.forEach((button) => {
            button.addEventListener("click", () => {
                state.filter = button.dataset.userKeyFilter || "all";
                filterButtons.forEach((item) => item.classList.toggle("is-active", item === button));
                applyFilters();
            });
        });

        searchInput.addEventListener("input", () => {
            state.query = searchInput.value.trim().toLowerCase();
            applyFilters();
        });

        cards.forEach((card) => {
            const editor = card.querySelector("[data-user-key-editor]");
            if (!editor) return;
            editor.addEventListener("toggle", () => {
                if (editor.open) {
                    closeOtherEditors(card);
                }
                syncCardState(card);
            });
            syncCardState(card);
        });

        directory?.addEventListener("click", (event) => {
            const button = event.target.closest("[data-user-key-expand]");
            if (!button) return;
            const card = button.closest("[data-user-key-card]");
            const editor = card?.querySelector("[data-user-key-editor]");
            if (!card || !editor) return;
            const nextOpen = !editor.open;
            if (nextOpen) {
                closeOtherEditors(card);
            }
            editor.open = nextOpen;
            syncCardState(card);
            if (nextOpen) {
                window.requestAnimationFrame(() => {
                    editor.scrollIntoView({ behavior: "smooth", block: "nearest" });
                });
            }
        });

        const initialEditingCard = document.querySelector(".user-key-object-card.is-editing");
        if (initialEditingCard) {
            closeOtherEditors(initialEditingCard);
            window.requestAnimationFrame(() => {
                initialEditingCard.scrollIntoView({ behavior: "smooth", block: "nearest" });
            });
        } else if (createPanel?.classList.contains("is-focused")) {
            window.requestAnimationFrame(() => {
                createPanel.scrollIntoView({ behavior: "smooth", block: "nearest" });
            });
        }

        applyFilters();
    }

    async function initUserHome() {
        renderTrendChart(document.getElementById("user-home-trend-chart"));
        initUserLiveMonitor();
        const helperShell = document.getElementById("user-home-helper-shell");
        const keySelect = document.getElementById("user-helper-key-select");
        const modelSelect = document.getElementById("user-helper-model-select");
        const headerValue = document.getElementById("user-helper-header-value");
        const providerValue = document.getElementById("user-helper-provider-value");
        const curlBlock = document.getElementById("user-helper-curl-block");
        const pythonBlock = document.getElementById("user-helper-python-block");
        const copyHeaderBtn = document.getElementById("user-helper-copy-header-btn");
        if (!helperShell || !keySelect || !modelSelect || !headerValue || !providerValue || !curlBlock || !pythonBlock || !copyHeaderBtn) {
            return;
        }
        const profiles = safeJsonParse(helperShell.dataset.integrationProfiles || "[]");
        const baseUrl = `${window.location.origin}/v1`;
        if (!Array.isArray(profiles) || !profiles.length) {
            headerValue.textContent = "当前没有可回显明文的 API Key";
            providerValue.textContent = "-";
            curlBlock.textContent = "当前没有可生成示例的 API Key";
            pythonBlock.textContent = "当前没有可生成示例的 API Key";
            copyHeaderBtn.disabled = true;
            return;
        }
        const renderHelper = () => {
            const profile = profiles.find((item) => String(item.api_key_id) === String(keySelect.value)) || profiles[0];
            const modelName = modelSelect.value || "gpt-4.1-mini";
            const header = `Authorization: Bearer ${profile.raw_api_key}`;
            headerValue.textContent = header;
            providerValue.textContent = (profile.allowed_provider_names || []).join("、") || "未配置";
            curlBlock.textContent = [
                `curl ${baseUrl}/responses \\`,
                '  -H "Content-Type: application/json" \\',
                `  -H "${header}" \\`,
                `  -d '{"model":"${modelName}","input":"ping"}'`,
            ].join("\n");
            pythonBlock.textContent = [
                "from openai import OpenAI",
                "",
                "client = OpenAI(",
                `    base_url="${baseUrl}",`,
                `    api_key=\"${profile.raw_api_key}\",`,
                ")",
                "",
                "resp = client.responses.create(",
                `    model=\"${modelName}\",`,
                "    input=\"ping\",",
                ")",
                "print(resp.output_text)",
            ].join("\n");
            copyHeaderBtn.dataset.copyText = header;
            copyHeaderBtn.disabled = false;
        };
        keySelect.addEventListener("change", renderHelper);
        modelSelect.addEventListener("change", renderHelper);
        renderHelper();
    }

    function initUserLiveMonitor() {
        const refreshBtn = document.getElementById("user-monitor-refresh-btn");
        if (!document.getElementById("user-home-monitor")) return;
        let loading = false;
        const load = async (manual = false) => {
            if (loading) return;
            loading = true;
            try {
                await refreshUserMonitor();
                if (manual) showToast("我的监控数据已刷新");
            } catch (error) {
                if (manual) showToast(error.message, "error");
            } finally {
                loading = false;
            }
        };
        refreshBtn?.addEventListener("click", () => load(true));
        load(false);
        const timer = window.setInterval(() => load(false), 30000);
        registerPageCleanup(() => window.clearInterval(timer));
    }

    async function refreshUserMonitor() {
        const [summary, timeSeries] = await Promise.all([
            api.get("/api/user/metrics/summary?window_minutes=60"),
            api.get("/api/user/metrics/timeseries?window_minutes=180&bucket_minutes=15"),
        ]);
        const metricItems = Array.isArray(summary.items) ? summary.items : [];
        const timeSeriesItems = Array.isArray(timeSeries.items) ? timeSeries.items : [];
        const overview = summarizeMetricItems(metricItems);
        const requestTotal = document.getElementById("user-monitor-request-total");
        if (requestTotal) requestTotal.textContent = `${formatMetricShort(timeSeriesItems.reduce((sum, item) => sum + Number(item.total_requests || 0), 0))} 次`;
        const costValue = document.getElementById("user-monitor-cost-value");
        if (costValue) costValue.textContent = `${formatMoney(overview.totalCost)} / ${formatMetricShort(overview.totalTokens)} Token`;
        const modelCount = document.getElementById("user-monitor-model-count");
        if (modelCount) modelCount.textContent = `${new Set(metricItems.map((item) => item.requested_model).filter(Boolean)).size} 个`;
        renderMonitorChart(document.getElementById("user-monitor-traffic-chart"), timeSeriesItems, {
            barKey: "total_requests",
            lineKey: "failed_requests",
            lineLabel: "失败",
            label: "我的请求量与失败趋势",
        });
        renderMonitorChart(document.getElementById("user-monitor-cost-chart"), timeSeriesItems, {
            barKey: "total_tokens",
            lineKey: "total_cost",
            lineLabel: "费用",
            label: "我的 Token 与费用趋势",
            width: 520,
            height: 220,
        });
        renderMonitorRank(document.getElementById("user-monitor-model-rank"), metricItems, { limit: 5 });
        updateRefreshLabel(document.getElementById("user-monitor-refresh-label"));
    }

    async function initUserBilling() {
        renderTrendChart(document.getElementById("user-billing-trend-chart"));
    }

    async function initUserLogs() {
        const tableBody = document.getElementById("logs-table-body");
        const refreshBtn = document.getElementById("logs-refresh-btn");
        const exportBtn = document.getElementById("logs-export-btn");
        const lastRefreshLabel = document.getElementById("logs-last-refresh");
        const providerSelect = document.getElementById("logs-provider-id");
        const modelSelect = document.getElementById("logs-model-name");
        const modelQueryInput = document.getElementById("logs-model-query");
        const apiClientKeyIdSelect = document.getElementById("logs-api-client-key-id");
        const apiClientKeyQuerySelect = document.getElementById("logs-api-client-key-query");
        const apiClientKeyQueryManualInput = document.getElementById("logs-api-client-key-query-manual");
        const tenantNameInput = document.getElementById("logs-tenant-name");
        const projectNameInput = document.getElementById("logs-project-name");
        const appNameInput = document.getElementById("logs-app-name");
        const environmentNameInput = document.getElementById("logs-environment-name");
        const excludeHealthChecksInput = document.getElementById("logs-exclude-health-checks");
        const pageSizeSelect = document.getElementById("logs-page-size");
        const pageMeta = document.getElementById("logs-page-meta");
        const prevPageBtn = document.getElementById("logs-prev-page-btn");
        const nextPageBtn = document.getElementById("logs-next-page-btn");
        const traceModal = document.getElementById("log-trace-modal");
        const traceContent = document.getElementById("log-trace-content");
        const closeBtn = document.getElementById("log-trace-close");
        const immediateFilterIds = [
            "logs-log-type",
            "logs-provider-id",
            "logs-model-name",
            "logs-api-client-key-id",
            "logs-api-client-key-query",
            "logs-success",
            "logs-tenant-name",
            "logs-project-name",
            "logs-app-name",
            "logs-environment-name",
        ];
        const debouncedFilterIds = [
            "logs-model-query",
            "logs-api-client-key-query-manual",
            "logs-conversation-key",
        ];
        if (
            !tableBody || !refreshBtn || !exportBtn || !lastRefreshLabel || !providerSelect || !modelSelect
            || !apiClientKeyIdSelect || !apiClientKeyQuerySelect || !excludeHealthChecksInput || !pageSizeSelect
            || !pageMeta || !prevPageBtn || !nextPageBtn || !traceModal || !traceContent || !closeBtn
        ) {
            return;
        }
        const currentParams = new URLSearchParams(window.location.search);
        const state = {
            page: Math.max(1, Number.parseInt(currentParams.get("page") || "1", 10) || 1),
            pageSize: [20, 50, 100, 200].includes(Number.parseInt(currentParams.get("page_size") || "20", 10))
                ? Number.parseInt(currentParams.get("page_size") || "20", 10)
                : 20,
            total: 0,
        };
        let initialFilterValuesApplied = false;
        if (currentParams.get("conversation_key")) {
            document.getElementById("logs-conversation-key").value = currentParams.get("conversation_key");
        }
        if (currentParams.get("model_query")) {
            modelQueryInput.value = currentParams.get("model_query");
        }
        if (currentParams.get("api_client_key_query_text")) {
            apiClientKeyQueryManualInput.value = currentParams.get("api_client_key_query_text");
        }
        if (currentParams.get("exclude_health_checks") === "false") {
            excludeHealthChecksInput.checked = false;
        }
        pageSizeSelect.value = String(state.pageSize);
        const closeModal = () => traceModal.classList.add("hidden");
        closeBtn.addEventListener("click", closeModal);
        traceModal.addEventListener("click", (event) => {
            if (event.target === traceModal) closeModal();
        });
        for (const id of immediateFilterIds) {
            document.getElementById(id).addEventListener("change", () => {
                state.page = 1;
                loadLogs();
            });
        }
        const debouncedLoadLogs = debounce(() => {
            state.page = 1;
            loadLogs();
        });
        for (const id of debouncedFilterIds) {
            document.getElementById(id).addEventListener("input", () => {
                state.page = 1;
                debouncedLoadLogs();
            });
        }
        pageSizeSelect.addEventListener("change", () => {
            state.pageSize = Number.parseInt(pageSizeSelect.value || "20", 10) || 20;
            state.page = 1;
            loadLogs();
        });
        prevPageBtn.addEventListener("click", async () => {
            if (state.page <= 1) return;
            state.page -= 1;
            await loadLogs();
        });
        nextPageBtn.addEventListener("click", async () => {
            const totalPages = Math.max(1, Math.ceil((state.total || 0) / state.pageSize));
            if (state.page >= totalPages) return;
            state.page += 1;
            await loadLogs();
        });
        exportBtn.addEventListener("click", () => {
            const params = new URLSearchParams();
            const logType = document.getElementById("logs-log-type").value;
            const providerId = providerSelect.value;
            const modelName = modelSelect.value;
            const modelQuery = modelQueryInput.value.trim();
            const apiClientKeyId = apiClientKeyIdSelect.value;
            const apiClientKeyQuery = apiClientKeyQueryManualInput.value.trim() || apiClientKeyQuerySelect.value;
            const success = document.getElementById("logs-success").value;
            const conversationKey = document.getElementById("logs-conversation-key").value.trim();
            const tenantName = tenantNameInput.value.trim();
            const projectName = projectNameInput.value.trim();
            const appName = appNameInput.value.trim();
            const environmentName = environmentNameInput.value.trim();
            if (logType) params.set("log_type", logType);
            if (providerId) params.set("provider_id", providerId);
            if (modelName) params.set("model_name", modelName);
            if (modelQuery) params.set("model_query", modelQuery);
            if (apiClientKeyId) params.set("api_client_key_id", apiClientKeyId);
            if (apiClientKeyQuery) params.set("api_client_key_query", apiClientKeyQuery);
            if (apiClientKeyQueryManualInput.value.trim()) params.set("api_client_key_query_text", apiClientKeyQueryManualInput.value.trim());
            if (success) params.set("success", success);
            if (conversationKey) params.set("conversation_key", conversationKey);
            if (tenantName) params.set("tenant_name", tenantName);
            if (projectName) params.set("project_name", projectName);
            if (appName) params.set("app_name", appName);
            if (environmentName) params.set("environment_name", environmentName);
            params.set("exclude_health_checks", excludeHealthChecksInput.checked ? "true" : "false");
            params.set("limit", "5000");
            setButtonTransientFeedback(exportBtn, "success", { successText: "准备导出" });
            window.location.href = `/user/logs/export?${params.toString()}`;
        });
        refreshBtn.addEventListener("click", async (event) => {
            event.preventDefault();
            await loadFilterOptions();
            await loadLogs({ manual: true });
        });

        function renderLogSummary(summary) {
            document.querySelectorAll("[data-log-summary]").forEach((node) => {
                const key = node.dataset.logSummary;
                node.textContent = formatNumber(summary?.[key] ?? 0);
            });
            document.querySelectorAll("[data-log-summary-cost]").forEach((node) => {
                const key = node.dataset.logSummaryCost;
                const value = summary?.[key];
                node.textContent = value == null || Number.isNaN(Number(value)) ? "0" : formatAdaptiveDecimal(value, { maxDecimals: 9, fallback: "0" });
            });
        }

        function formatMetricValue(value, suffix = "") {
            if (value == null || Number.isNaN(Number(value))) return "-";
            return `${formatNumber(Number(value))}${suffix}`;
        }

        function formatRateValue(value) {
            if (value == null || Number.isNaN(Number(value))) return "-";
            return Number(value).toFixed(2);
        }

        function buildSessionValue(log) {
            return log.session_id || log.conversation_key || log.request_id || "-";
        }

        function renderSelectOptions(selectNode, items, emptyLabel = "全部") {
            const previousValue = selectNode.value;
            selectNode.innerHTML = [`<option value="">${escapeHtml(emptyLabel)}</option>`]
                .concat(
                    (items || []).map((item) => (
                        `<option value="${escapeHtml(item.value)}">${escapeHtml(item.label)}</option>`
                    ))
                )
                .join("");
            if (previousValue && Array.from(selectNode.options).some((option) => option.value === previousValue)) {
                selectNode.value = previousValue;
            }
        }

        async function loadFilterOptions() {
            const params = new URLSearchParams({
                exclude_health_checks: excludeHealthChecksInput.checked ? "true" : "false",
                _ts: Date.now().toString(),
            });
            const data = await api.get(`/api/user/logs/filter-options?${params.toString()}`);
            renderSelectOptions(providerSelect, data.providers);
            renderSelectOptions(modelSelect, data.model_names);
            renderSelectOptions(apiClientKeyIdSelect, data.api_client_key_ids);
            renderSelectOptions(apiClientKeyQuerySelect, data.api_client_key_queries);
            renderSelectOptions(tenantNameInput, data.tenants);
            renderSelectOptions(projectNameInput, data.projects);
            renderSelectOptions(appNameInput, data.apps);
            renderSelectOptions(environmentNameInput, data.environments);
            if (!initialFilterValuesApplied && currentParams.get("provider_id")) {
                providerSelect.value = currentParams.get("provider_id");
            }
            if (!initialFilterValuesApplied && currentParams.get("model_name")) {
                modelSelect.value = currentParams.get("model_name");
            }
            if (!initialFilterValuesApplied && currentParams.get("api_client_key_id")) {
                apiClientKeyIdSelect.value = currentParams.get("api_client_key_id");
            }
            if (!initialFilterValuesApplied && currentParams.get("api_client_key_query")) {
                apiClientKeyQuerySelect.value = currentParams.get("api_client_key_query");
            }
            if (!initialFilterValuesApplied && currentParams.get("tenant_name")) {
                tenantNameInput.value = currentParams.get("tenant_name");
            }
            if (!initialFilterValuesApplied && currentParams.get("project_name")) {
                projectNameInput.value = currentParams.get("project_name");
            }
            if (!initialFilterValuesApplied && currentParams.get("app_name")) {
                appNameInput.value = currentParams.get("app_name");
            }
            if (!initialFilterValuesApplied && currentParams.get("environment_name")) {
                environmentNameInput.value = currentParams.get("environment_name");
            }
            if (!initialFilterValuesApplied && currentParams.get("log_type")) {
                document.getElementById("logs-log-type").value = currentParams.get("log_type");
            }
            if (!initialFilterValuesApplied && currentParams.get("success")) {
                document.getElementById("logs-success").value = currentParams.get("success");
            }
            initialFilterValuesApplied = true;
        }

        function renderLogPagination(total) {
            state.total = Number(total || 0);
            const totalPages = Math.max(1, Math.ceil(state.total / state.pageSize));
            if (state.page > totalPages) {
                state.page = totalPages;
            }
            pageMeta.textContent = `第 ${formatNumber(state.page)} 页，共 ${formatNumber(totalPages)} 页 · 共 ${formatNumber(state.total)} 条`;
            prevPageBtn.disabled = state.page <= 1;
            nextPageBtn.disabled = state.page >= totalPages;
        }

        async function loadLogs({ manual = false } = {}) {
            setButtonLoading(refreshBtn, true);
            const params = new URLSearchParams({
                page: String(state.page),
                page_size: String(state.pageSize),
            });
            const logType = document.getElementById("logs-log-type").value;
            const providerId = providerSelect.value;
            const modelName = modelSelect.value;
            const modelQuery = modelQueryInput.value.trim();
            const apiClientKeyId = apiClientKeyIdSelect.value;
            const apiClientKeyQuery = apiClientKeyQueryManualInput.value.trim() || apiClientKeyQuerySelect.value;
            const success = document.getElementById("logs-success").value;
            const conversationKey = document.getElementById("logs-conversation-key").value.trim();
            const tenantName = tenantNameInput.value.trim();
            const projectName = projectNameInput.value.trim();
            const appName = appNameInput.value.trim();
            const environmentName = environmentNameInput.value.trim();
            const excludeHealthChecks = excludeHealthChecksInput.checked;
            if (logType) params.set("log_type", logType);
            if (providerId) params.set("provider_id", providerId);
            if (modelName) params.set("model_name", modelName);
            if (modelQuery) params.set("model_query", modelQuery);
            if (apiClientKeyId) params.set("api_client_key_id", apiClientKeyId);
            if (apiClientKeyQuery) params.set("api_client_key_query", apiClientKeyQuery);
            if (success) params.set("success", success);
            if (conversationKey) params.set("conversation_key", conversationKey);
            if (tenantName) params.set("tenant_name", tenantName);
            if (projectName) params.set("project_name", projectName);
            if (appName) params.set("app_name", appName);
            if (environmentName) params.set("environment_name", environmentName);
            params.set("exclude_health_checks", excludeHealthChecks ? "true" : "false");
            params.set("_ts", Date.now().toString());
            try {
                const data = await api.get(`/api/user/logs?${params.toString()}`);
                renderLogPagination(data.total ?? data.items.length);
                renderLogSummary(data.summary || {});
                tableBody.innerHTML = data.items.map((log) => `
                    <tr>
                        <td>
                            <strong>${formatDate(log.created_at)}</strong>
                            <div class="table-muted">${escapeHtml(log.http_method || "-")}</div>
                        </td>
                        <td>
                            <strong>${escapeHtml(formatLogTypeLabel(log.log_type))}</strong>
                            <div class="table-muted">思维等级 ${escapeHtml(log.reasoning_level || "无")}${log.model_reasoning_effort ? ` · 参数 ${escapeHtml(log.model_reasoning_effort)}` : ""}</div>
                        </td>
                        <td>
                            <strong>${escapeHtml(log.api_client_key_name || "-")}</strong>
                            <div class="table-muted">${escapeHtml(log.api_client_key_prefix || "-")}</div>
                        </td>
                        <td>${escapeHtml(buildSessionValue(log))}</td>
                        <td>${escapeHtml(log.requested_model || log.model_name || "-")}</td>
                        <td>${escapeHtml(log.provider_name || "-")}</td>
                        <td>${renderLogResultCell(log)}</td>
                        <td>${renderLogBillingCell(log)}</td>
                        <td>
                            <strong>${formatMetricValue(log.duration_ms ?? log.latency_ms, " ms")}</strong>
                            <div class="table-muted">TTFB ${formatMetricValue(log.ttfb_ms, " ms")} · TPS ${formatRateValue(log.tps)}</div>
                        </td>
                        <td>
                            <div class="table-actions">
                                <button class="table-action-btn" data-action="show-trace" data-log-id="${log.id}">详情</button>
                                ${log.conversation_key ? `<button class="table-action-btn" data-action="open-conversation" data-conversation-key="${encodeURIComponent(log.conversation_key)}">回放</button>` : ""}
                            </div>
                        </td>
                    </tr>
                `).join("") || '<tr><td colspan="10"><div class="empty-state">暂无日志</div></td></tr>';
                tableBody.querySelectorAll('button[data-action="show-trace"]').forEach((button, index) => {
                    button.dataset.log = JSON.stringify(data.items[index] || {});
                });
                enhanceInteractiveButtons(tableBody);
                lastRefreshLabel.textContent = `最近刷新: ${formatDate(new Date().toISOString())}`;
                if (manual) {
                    showToast(`日志已刷新，第 ${state.page} 页 / ${Math.max(1, Math.ceil((state.total || 0) / state.pageSize))} 页`);
                }
            } catch (error) {
                showToast(error.message, "error");
            } finally {
                setButtonLoading(refreshBtn, false);
            }
        }

        excludeHealthChecksInput.addEventListener("change", async () => {
            state.page = 1;
            await loadFilterOptions();
            await loadLogs();
        });

        tableBody.addEventListener("click", (event) => {
            const button = event.target.closest('button[data-action]');
            if (!button) return;
            if (button.dataset.action === "open-conversation") {
                const conversationKey = decodeURIComponent(button.dataset.conversationKey);
                const target = `/user/conversations?conversation_key=${encodeURIComponent(conversationKey)}`;
                navigateWithinShell(target).catch(() => {
                    window.location.href = target;
                });
                return;
            }
            if (button.dataset.action !== "show-trace") return;
            const log = safeJsonParse(button.dataset.log || "") || {};
            const detail = {
                id: log.id,
                log_type: log.log_type,
                request_id: log.request_id,
                conversation_key: log.conversation_key,
                session_id: log.session_id,
                requested_model: log.requested_model,
                model_name: log.model_name,
                provider_name: log.provider_name,
                user_account_id: log.user_account_id,
                user_account_name: log.user_account_name,
                api_client_key_id: log.api_client_key_id,
                api_client_key_name: log.api_client_key_name,
                api_client_key_prefix: log.api_client_key_prefix,
                api_client_auth_result: log.api_client_auth_result,
                api_client_remaining_tokens: log.api_client_remaining_tokens,
                http_method: log.http_method,
                reasoning_level: log.reasoning_level,
                model_reasoning_effort: log.model_reasoning_effort,
                attempt_count: log.attempt_count,
                success: log.success,
                status_code: log.status_code,
                latency_ms: log.latency_ms,
                ttfb_ms: log.ttfb_ms,
                duration_ms: log.duration_ms,
                tps: log.tps,
                prompt_tokens: log.prompt_tokens,
                completion_tokens: log.completion_tokens,
                total_tokens: log.total_tokens,
                cache_read_tokens: log.cache_read_tokens,
                cache_write_tokens: log.cache_write_tokens,
                billing_multiplier: log.billing_multiplier,
                channel_price_input_per_1k: log.channel_price_input_per_1k,
                channel_price_output_per_1k: log.channel_price_output_per_1k,
                channel_price_cache_per_1k: log.channel_price_cache_per_1k,
                prompt_cost: log.prompt_cost,
                completion_cost: log.completion_cost,
                total_cost: log.total_cost,
                billing_calculation: formatBillingCalculation(log),
                finish_reason: log.finish_reason,
                upstream_request_id: log.upstream_request_id,
                request_body_json: safeJsonParse(log.request_body_json || "") ?? log.request_body_json,
                response_body_json: safeJsonParse(log.response_body_json || "") ?? log.response_body_json,
                response_text: log.response_text,
                trace: safeJsonParse(log.trace_json || "") ?? log.trace_json,
                created_at: log.created_at,
            };
            traceContent.textContent = JSON.stringify(detail, null, 2);
            traceModal.classList.remove("hidden");
        });

        await loadFilterOptions();
        await loadLogs();
    }

    async function initUserConversations() {
        const listContainer = document.getElementById("conversation-list");
        const timeline = document.getElementById("conversation-timeline");
        const summary = document.getElementById("conversation-summary");
        const searchInput = document.getElementById("conversation-search");
        const refreshBtn = document.getElementById("conversation-refresh-btn");
        const openLogsLink = document.getElementById("conversation-open-logs");
        const title = document.getElementById("conversation-detail-title");
        const totalCountPrimary = document.getElementById("conversation-total-count");
        const totalCountSecondary = document.getElementById("conversation-total-count-secondary");
        const totalRequestsNode = document.getElementById("conversation-total-requests");
        const totalTokensNode = document.getElementById("conversation-total-tokens");
        const activeQueryNode = document.getElementById("conversation-active-query");
        const resultCountNode = document.getElementById("conversation-result-count");
        const lastUpdatedNode = document.getElementById("conversation-last-updated");
        const selectedProviderNode = document.getElementById("conversation-selected-provider");
        const selectedUpdatedNode = document.getElementById("conversation-selected-updated");
        if (
            !listContainer || !timeline || !summary || !searchInput || !refreshBtn || !openLogsLink || !title
            || !totalCountPrimary || !totalCountSecondary || !totalRequestsNode || !totalTokensNode
            || !activeQueryNode || !resultCountNode || !lastUpdatedNode || !selectedProviderNode || !selectedUpdatedNode
        ) {
            return;
        }
        const currentParams = new URLSearchParams(window.location.search);
        const state = {
            items: [],
            activeKey: null,
            page: 1,
            pageSize: 100,
            total: 0,
        };
        if (currentParams.get("query")) {
            searchInput.value = currentParams.get("query");
        }

        function renderConversationOverview() {
            const totalCount = state.total || state.items.length;
            const totalRequests = state.items.reduce((sum, item) => sum + Number(item.request_count || 0), 0);
            const totalTokens = state.items.reduce((sum, item) => sum + Number(item.total_tokens || 0), 0);
            const latestUpdatedAt = state.items[0]?.updated_at || null;
            const queryText = searchInput.value.trim();
            totalCountPrimary.textContent = formatNumber(totalCount);
            totalCountSecondary.textContent = formatNumber(totalCount);
            totalRequestsNode.textContent = formatNumber(totalRequests);
            totalTokensNode.textContent = formatNumber(totalTokens);
            activeQueryNode.textContent = queryText ? `当前检索：${queryText}` : "当前展示全部会话";
            resultCountNode.textContent = `${formatNumber(state.items.length)} 条结果`;
            lastUpdatedNode.textContent = `最近更新 ${formatDate(latestUpdatedAt)}`;
        }

        function renderConversationList() {
            listContainer.innerHTML = state.items.map((item) => `
                <button class="conversation-item ${state.activeKey === item.conversation_key ? "active" : ""}" data-conversation-key="${encodeURIComponent(item.conversation_key)}">
                    <div class="conversation-item-top">
                        <strong>${escapeHtml(item.conversation_key)}</strong>
                        <span>${item.request_count} 次</span>
                    </div>
                    <div class="conversation-item-meta">
                        <span>${escapeHtml(item.latest_model || "-")}</span>
                        <span>${item.total_tokens} tokens</span>
                    </div>
                    <p>${escapeHtml(item.preview_text || "暂无回复预览")}</p>
                    <div class="conversation-item-foot">
                        <span>${formatDate(item.updated_at)}</span>
                        <span>${escapeHtml(item.latest_provider_name || "-")}</span>
                    </div>
                </button>
            `).join("") || '<div class="empty-state">暂无会话记录</div>';
            enhanceInteractiveButtons(listContainer);
        }

        function resetConversationDetail() {
            state.activeKey = null;
            title.textContent = "选择左侧会话";
            summary.innerHTML = '<div class="empty-state">选择一个会话后，可查看完整回放、命中线路与 token 使用。</div>';
            timeline.innerHTML = "";
            openLogsLink.classList.add("hidden");
            selectedProviderNode.textContent = "-";
            selectedUpdatedNode.textContent = "-";
            renderConversationList();
        }

        async function openConversation(conversationKey, { replace = false } = {}) {
            try {
                const detail = await api.get(`/api/user/conversations/${encodeURIComponent(conversationKey)}`);
                state.activeKey = detail.conversation_key;
                title.textContent = detail.conversation_key;
                summary.innerHTML = `
                    <div class="conversation-summary-grid">
                        <article class="conversation-summary-card">
                            <span>请求数</span>
                            <strong>${detail.request_count}</strong>
                        </article>
                        <article class="conversation-summary-card">
                            <span>成功 / 失败</span>
                            <strong>${detail.success_count} / ${detail.failure_count}</strong>
                        </article>
                        <article class="conversation-summary-card">
                            <span>总 Token</span>
                            <strong>${detail.total_tokens}</strong>
                        </article>
                        <article class="conversation-summary-card">
                            <span>最近模型</span>
                            <strong>${escapeHtml(detail.latest_model || "-")}</strong>
                        </article>
                    </div>
                    <div class="conversation-meta-bar">
                        <span>首条时间 ${formatDate(detail.started_at)}</span>
                        <span>最近更新时间 ${formatDate(detail.updated_at)}</span>
                        <span>最近命中线路 ${escapeHtml(detail.latest_provider_name || "-")}</span>
                    </div>
                `;
                timeline.innerHTML = detail.turns.map((turn) => `
                    <article class="conversation-turn conversation-turn-${escapeHtml(turn.role)}">
                        <div class="conversation-turn-head">
                            <div>
                                <span class="conversation-role">${escapeHtml(turn.role)}</span>
                                <strong>${escapeHtml(turn.provider_name || turn.requested_model || "-")}</strong>
                            </div>
                            <div class="conversation-turn-meta">
                                <span>${formatDate(turn.created_at)}</span>
                                <span>${turn.total_tokens ?? "-"} tokens</span>
                                <span>${turn.is_stream ? "stream" : "json"}</span>
                                <span>${turn.has_image ? "vision" : "text"}</span>
                            </div>
                        </div>
                        <pre class="conversation-turn-body">${escapeHtml(turn.content || "")}</pre>
                    </article>
                `).join("") || '<div class="empty-state">该会话暂无可回放内容</div>';
                openLogsLink.href = `/user/logs?conversation_key=${encodeURIComponent(detail.conversation_key)}`;
                openLogsLink.classList.remove("hidden");
                selectedProviderNode.textContent = detail.latest_provider_name || "-";
                selectedUpdatedNode.textContent = formatDate(detail.updated_at);
                renderConversationList();
                const params = new URLSearchParams();
                if (searchInput.value.trim()) params.set("query", searchInput.value.trim());
                params.set("conversation_key", detail.conversation_key);
                const url = `/user/conversations?${params.toString()}`;
                if (replace) {
                    window.history.replaceState({ path: url }, "", url);
                } else {
                    window.history.pushState({ path: url }, "", url);
                }
                updateActiveNavigation("/user/conversations");
            } catch (error) {
                showToast(error.message, "error");
            }
        }

        async function loadConversations(preferredKey = null) {
            try {
                setButtonLoading(refreshBtn, true);
                const params = new URLSearchParams({
                    page: String(state.page),
                    page_size: String(state.pageSize),
                });
                const query = searchInput.value.trim();
                if (query) params.set("query", query);
                const data = await api.get(`/api/user/conversations?${params.toString()}`);
                state.items = data.items || [];
                state.total = Number(data.total || state.items.length);
                renderConversationOverview();
                renderConversationList();
                const urlKey = new URLSearchParams(window.location.search).get("conversation_key");
                const nextKey = preferredKey || urlKey || state.activeKey || state.items[0]?.conversation_key || null;
                if (nextKey) {
                    await openConversation(nextKey, { replace: true });
                } else {
                    resetConversationDetail();
                }
            } catch (error) {
                showToast(error.message, "error");
            } finally {
                setButtonLoading(refreshBtn, false);
            }
        }

        listContainer.addEventListener("click", async (event) => {
            const button = event.target.closest("[data-conversation-key]");
            if (!button) return;
            await openConversation(decodeURIComponent(button.dataset.conversationKey));
        });
        searchInput.addEventListener("input", async () => {
            state.page = 1;
            await loadConversations();
        });
        refreshBtn.addEventListener("click", async () => {
            try {
                setButtonLoading(refreshBtn, true);
                await loadConversations(state.activeKey);
                setButtonTransientFeedback(refreshBtn, "success", { successText: "已刷新" });
            } catch (error) {
                showToast(error.message, "error");
                setButtonTransientFeedback(refreshBtn, "error", { errorText: "刷新失败" });
            } finally {
                setButtonLoading(refreshBtn, false);
            }
        });

        await loadConversations();
    }

    function deriveUserSelfTestResolution(result = {}) {
        const success = Boolean(result.success);
        const message = String(result.message || "");
        const code = String(result.code || "");
        const statusCode = Number(result.status_code || 0);
        const normalized = `${code} ${message} ${JSON.stringify(result.trace || "")}`.toLowerCase();
        const summary = result.summary || {};
        const scenarios = Array.isArray(result.scenarios) ? result.scenarios : [];
        const failedScenarios = scenarios.filter((item) => item && item.success === false);
        const failedLabels = failedScenarios.slice(0, 3).map((item) => item.scenario_label || item.endpoint_path || "未命名场景");

        if (success) {
            return {
                failure: [
                    { label: "状态", value: "本次测试成功" },
                    { label: "摘要", value: `本次共完成 ${summary.total_scenarios || scenarios.length || 0} 个自检场景，全部通过，可以进入小流量正式接入。` },
                ],
                fixes: [
                    { label: "建议", value: "保持当前 API Key、模型和 Base URL 组合，不要在正式接入前再随意替换。" },
                    { label: "补充", value: "若是生产接入，建议先小流量验证，再观察日志页的费用和延迟。" },
                ],
                next: [
                    { label: "下一步", value: "回到首页复制接入信息，或直接把当前组合写入你的业务服务。" },
                    { label: "排障入口", value: "若后续结果异常，去“我的日志”或“会话回放”核对请求体与返回内容。" },
                ],
            };
        }

        if (failedScenarios.length > 1) {
            return {
                failure: [
                    { label: "状态", value: "多场景自检失败" },
                    { label: "摘要", value: `本次共有 ${summary.failed_scenarios || failedScenarios.length} 个场景失败：${failedLabels.join("、")}` },
                ],
                fixes: [
                    { label: "修复建议", value: "先看下方“场景明细”，确认是仅某个端点失败，还是文本、流式、图片整体都失败。" },
                    { label: "补充检查", value: "若只有图片链路失败，优先排查模型视觉能力和图片输入格式；若流式单独失败，优先排查渠道对 SSE 的兼容性。" },
                ],
                next: [
                    { label: "下一步", value: "下载本次自检报告，带着失败场景和调度链路去日志页或联系管理员定位。" },
                ],
            };
        }

        if (normalized.includes("无法回显明文") || normalized.includes("decrypt") || normalized.includes("轮换")) {
            return {
                failure: [
                    { label: "状态", value: "当前密钥不能直接用于真实测试" },
                    { label: "摘要", value: "这把 API Key 没有可回显的明文，浏览器侧无法代你发起 Bearer 测试。" },
                ],
                fixes: [
                    { label: "修复建议", value: "到 API Key 管理页或密钥控制台执行轮换，生成新的可复制明文密钥。" },
                    { label: "修复后", value: "重新复制明文和 Authorization Header，再回到自检页重试。" },
                ],
                next: [
                    { label: "下一步", value: "打开 API Key 管理页，轮换当前密钥后再回来测试。" },
                ],
            };
        }

        if (normalized.includes("auth") || normalized.includes("鉴权") || normalized.includes("api key") || code === "api_key_invalid") {
            return {
                failure: [
                    { label: "状态", value: "鉴权未通过" },
                    { label: "摘要", value: "当前 Bearer 密钥不可用，可能已失效、被停用或复制错误。" },
                ],
                fixes: [
                    { label: "修复建议", value: "回到 API Key 管理页确认密钥状态是否为正常可用，并重新复制明文密钥。" },
                    { label: "补充检查", value: "若刚轮换过密钥，请同步更新外部系统中的旧环境变量。" },
                ],
                next: [
                    { label: "下一步", value: "先修复密钥状态，再用同一模型重新跑一次真实测试。" },
                ],
            };
        }

        if (normalized.includes("model") || normalized.includes("模型")) {
            return {
                failure: [
                    { label: "状态", value: "模型不可用或未授权" },
                    { label: "摘要", value: "当前密钥命中的授权范围里没有这个模型，或者所选模型暂不可用。" },
                ],
                fixes: [
                    { label: "修复建议", value: "去可用模型页确认模型名是否存在，再检查这把密钥的授权中转站是否覆盖该模型。" },
                    { label: "补充检查", value: "若模型名是手工输入或从别处复制的，优先改成站内模型页展示的标准名称。" },
                ],
                next: [
                    { label: "下一步", value: "回到可用模型页确认模型名，再重新选择模型执行自检。" },
                ],
            };
        }

        if (normalized.includes("unbound") || normalized.includes("中转") || normalized.includes("provider") || normalized.includes("route")) {
            return {
                failure: [
                    { label: "状态", value: "授权线路不可用" },
                    { label: "摘要", value: "当前密钥没有可命中的中转站，或默认路由和授权集合无法组成候选线路。" },
                ],
                fixes: [
                    { label: "修复建议", value: "回到 API Key 管理页，检查授权中转站、默认中转站和路由模式是否配置正确。" },
                    { label: "补充检查", value: "若是手动模式，确认是否需要开启回退，避免单线路失败后没有候选线路。" },
                ],
                next: [
                    { label: "下一步", value: "修正密钥授权后，重新查看候选线路预览并再跑一次真实测试。" },
                ],
            };
        }

        if (normalized.includes("quota") || normalized.includes("余额") || normalized.includes("balance") || normalized.includes("额度")) {
            return {
                failure: [
                    { label: "状态", value: "额度或余额不足" },
                    { label: "摘要", value: "当前账号或密钥已触发余额、费用或配额限制，代理链路被提前拦截。" },
                ],
                fixes: [
                    { label: "修复建议", value: "先去账单页核对共享余额和累计消费，再确认是否触发日/月或总额度限制。" },
                    { label: "补充检查", value: "若是账户级限额导致，需联系管理员调整配额或充值。" },
                ],
                next: [
                    { label: "下一步", value: "补足余额或解除额度限制后，再使用同一组参数重新测试。" },
                ],
            };
        }

        if ([502, 503, 504].includes(statusCode) || normalized.includes("timeout") || normalized.includes("上游") || normalized.includes("超时")) {
            return {
                failure: [
                    { label: "状态", value: "上游线路异常或延迟过高" },
                    { label: "摘要", value: "当前命中的渠道可能健康异常、响应超时或暂时不可用。" },
                ],
                fixes: [
                    { label: "修复建议", value: "先看候选线路列表里的健康状态和成功率，必要时换模型或换一把授权范围更广的密钥重试。" },
                    { label: "补充检查", value: "如果连续失败，去日志页确认是否同一渠道持续报错。" },
                ],
                next: [
                    { label: "下一步", value: "保留当前报错信息，换一路径或稍后重试，并观察日志中心是否出现集中故障。" },
                ],
            };
        }

        return {
            failure: [
                { label: "状态", value: "测试失败" },
                { label: "摘要", value: message || "当前请求未通过，请结合调度链路和原始返回继续排查。" },
            ],
            fixes: [
                { label: "修复建议", value: "优先检查密钥状态、模型名、授权中转站和余额，再查看调度链路里的具体报错。" },
            ],
            next: [
                { label: "下一步", value: "若无法直接判断原因，去“我的日志”或“会话回放”定位完整上下文。" },
            ],
        };
    }

    function updateUserSelfTestImageFields() {
        const imageModeSelect = document.getElementById("user-self-test-image-mode");
        const imageDetailSelect = document.getElementById("user-self-test-image-detail");
        const imageUrlField = document.getElementById("user-self-test-image-url-field");
        const imageUrlInput = document.getElementById("user-self-test-image-url");
        const imageFileField = document.getElementById("user-self-test-image-file-field");
        const imageFileInput = document.getElementById("user-self-test-image-file");
        const imageNote = document.getElementById("user-self-test-image-note");
        if (!imageModeSelect || !imageDetailSelect || !imageUrlField || !imageUrlInput || !imageFileField || !imageFileInput || !imageNote) {
            return;
        }
        const mode = imageModeSelect.value || "none";
        const detailText = describeImageDetailMode(imageDetailSelect.value || "auto");
        const constraintText = getImageUploadConstraintText();
        imageUrlField.classList.toggle("hidden", mode !== "url");
        imageFileField.classList.toggle("hidden", mode !== "upload");
        if (mode === "url") {
            imageNote.textContent = `将额外补测图片链路，并把当前图片链接按标准视觉请求发送到内部自检接口。${detailText} ${constraintText}`;
            return;
        }
        if (mode === "upload") {
            const file = imageFileInput.files?.[0];
            const fileError = file ? validateImageUploadFile(file) : null;
            imageNote.textContent = file
                ? `将把本地图片通过内部 Session 自检接口发送，并追加视觉场景探测：${file.name}。${fileError || detailText} ${constraintText}`
                : `将把本地图片通过内部 Session 自检接口发送，并追加视觉场景探测。${detailText} ${constraintText}`;
            return;
        }
        imageNote.textContent = `当前默认只测试文本链路。若切换为图片链接或本地图片，将追加图片场景自检。${detailText} ${constraintText}`;
    }

    function summarizeUserSelfTestScenarios(data = {}) {
        const summary = data.summary || {};
        const scenarios = Array.isArray(data.scenarios) ? data.scenarios : [];
        return {
            total: Number(summary.total_scenarios || scenarios.length || 0),
            success: Number(summary.success_scenarios || scenarios.filter((item) => item?.success).length || 0),
            failed: Number(summary.failed_scenarios || scenarios.filter((item) => item && item.success === false).length || 0),
            imageEnabled: Boolean(summary.image_enabled),
        };
    }

    function renderUserSelfTestScenarios(target, scenarios = []) {
        if (!target) return;
        if (!scenarios.length) {
            target.innerHTML = '<tr><td colspan="7"><div class="empty-state">执行真实测试后，这里会展开每个场景的结果。</div></td></tr>';
            return;
        }
        target.innerHTML = scenarios.map((item) => `
            <tr>
                <td>${escapeHtml(item.scenario_label || "-")}</td>
                <td>${escapeHtml(item.endpoint_path || "-")}</td>
                <td>${item.has_image ? "图片" : "文本"} / ${item.stream ? "流式" : "非流式"}</td>
                <td>${item.success ? "成功" : "失败"}</td>
                <td>${escapeHtml(item.provider_name || "-")}</td>
                <td>${item.latency_ms ?? "-"}</td>
                <td>${escapeHtml(item.message || "-")}</td>
            </tr>
        `).join("");
        scheduleResponsiveTableSync(target.closest(".table-shell") || document);
    }

    function buildUserSelfTestReport(data = {}, options = {}) {
        const summary = summarizeUserSelfTestScenarios(data);
        const scenarios = Array.isArray(data.scenarios) ? data.scenarios : [];
        const lines = [
            "# aotu-gpt 接入自检报告",
            "",
            `- 生成时间：${formatDate(new Date().toISOString())}`,
            `- API Key：${options.apiKeyLabel || "-"}`,
            `- 模型：${options.modelName || data.model_name || "-"}`,
            `- 图片链路：${summary.imageEnabled ? "已启用" : "未启用"}`,
            `- 场景总数：${summary.total}`,
            `- 成功场景：${summary.success}`,
            `- 失败场景：${summary.failed}`,
            `- 总体结果：${data.success ? "通过" : "未全部通过"}`,
            "",
            "## 场景明细",
            "",
        ];
        scenarios.forEach((item, index) => {
            lines.push(`### ${index + 1}. ${item.scenario_label || item.endpoint_path || "未命名场景"}`);
            lines.push(`- 端点：${item.endpoint_path || "-"}`);
            lines.push(`- 类型：${item.has_image ? "图片" : "文本"} / ${item.stream ? "流式" : "非流式"}`);
            lines.push(`- 状态：${item.success ? "成功" : "失败"}`);
            lines.push(`- 渠道：${item.provider_name || "-"}`);
            lines.push(`- 状态码：${item.status_code ?? "-"}`);
            lines.push(`- 耗时：${item.latency_ms ?? "-"} ms`);
            lines.push(`- 摘要：${item.message || "-"}`);
            if (item.output_text) {
                lines.push("- 输出预览：");
                lines.push("```text");
                lines.push(String(item.output_text));
                lines.push("```");
            }
            if (item.trace) {
                lines.push("- 调度链路：");
                lines.push("```json");
                lines.push(JSON.stringify(item.trace, null, 2));
                lines.push("```");
            }
            lines.push("");
        });
        return lines.join("\n");
    }

    async function initUserSelfTest() {
        const runBtn = document.getElementById("user-self-test-run-btn");
        const keySelect = document.querySelector('form[action="/user/self-test"] select[name="api_key_id"]');
        const modelSelect = document.querySelector('form[action="/user/self-test"] select[name="model_name"]');
        const summary = document.getElementById("user-self-test-result-summary");
        const failure = document.getElementById("user-self-test-failure");
        const fixes = document.getElementById("user-self-test-fixes");
        const next = document.getElementById("user-self-test-next");
        const output = document.getElementById("user-self-test-output");
        const trace = document.getElementById("user-self-test-trace");
        const imageModeSelect = document.getElementById("user-self-test-image-mode");
        const imageDetailSelect = document.getElementById("user-self-test-image-detail");
        const imageUrlInput = document.getElementById("user-self-test-image-url");
        const imageFileInput = document.getElementById("user-self-test-image-file");
        const scenarioTableBody = document.getElementById("user-self-test-scenarios");
        const downloadBtn = document.getElementById("user-self-test-download-btn");
        let lastSelfTestResult = null;
        if (
            !runBtn || !keySelect || !modelSelect || !summary || !failure || !fixes || !next || !output || !trace
            || !imageModeSelect || !imageDetailSelect || !imageUrlInput || !imageFileInput || !scenarioTableBody || !downloadBtn
        ) {
            return;
        }

        function renderResolution(target, items = []) {
            target.innerHTML = items.map((item) => `
                <div><span>${escapeHtml(item.label || "-")}</span><strong>${escapeHtml(item.value || "-")}</strong></div>
            `).join("");
        }

        function resetDownloadButton() {
            downloadBtn.disabled = true;
            downloadBtn.dataset.ready = "false";
        }

        imageModeSelect.addEventListener("change", updateUserSelfTestImageFields);
        imageDetailSelect.addEventListener("change", updateUserSelfTestImageFields);
        imageFileInput.addEventListener("change", updateUserSelfTestImageFields);
        updateUserSelfTestImageFields();
        renderUserSelfTestScenarios(scenarioTableBody, []);
        resetDownloadButton();

        downloadBtn.addEventListener("click", async () => {
            if (!lastSelfTestResult) {
                showToast("请先执行一次真实测试", "error");
                return;
            }
            const apiKeyLabel = keySelect.options[keySelect.selectedIndex]?.textContent?.trim() || "";
            const reportText = buildUserSelfTestReport(lastSelfTestResult, {
                apiKeyLabel,
                modelName: modelSelect.value,
            });
            const blob = new Blob([reportText], { type: "text/markdown;charset=utf-8" });
            const url = URL.createObjectURL(blob);
            const link = document.createElement("a");
            link.href = url;
            link.download = `user-self-test-${modelSelect.value || "model"}-${new Date().toISOString().replace(/[:.]/g, "-")}.md`;
            document.body.appendChild(link);
            link.click();
            link.remove();
            URL.revokeObjectURL(url);
            showToast("自检报告已开始下载");
        });

        runBtn.addEventListener("click", async () => {
            if (!keySelect.value || !modelSelect.value) {
                showToast("请先选择 API Key 和模型", "error");
                return;
            }
            try {
                setButtonLoading(runBtn, true);
                summary.innerHTML = '<div><span>状态</span><strong>测试中...</strong></div>';
                renderResolution(failure, [{ label: "状态", value: "测试中" }, { label: "摘要", value: "正在发起最短真实调用，请稍候。" }]);
                renderResolution(fixes, [{ label: "建议", value: "等待当前测试返回结果。" }]);
                renderResolution(next, [{ label: "动作", value: "测试完成后，这里会生成下一步动作。" }]);
                output.textContent = "测试中...";
                trace.textContent = "测试中...";
                renderUserSelfTestScenarios(scenarioTableBody, []);
                lastSelfTestResult = null;
                resetDownloadButton();
                const formData = new FormData();
                formData.append("api_key_id", keySelect.value);
                formData.append("model_name", modelSelect.value);
                formData.append("image_mode", imageModeSelect.value || "none");
                formData.append("image_detail", imageDetailSelect.value || "auto");
                if ((imageModeSelect.value || "none") === "url" && imageUrlInput.value.trim()) {
                    formData.append("image_url", imageUrlInput.value.trim());
                }
                if ((imageModeSelect.value || "none") === "upload" && imageFileInput.files?.[0]) {
                    const fileError = validateImageUploadFile(imageFileInput.files[0]);
                    if (fileError) {
                        throw new Error(fileError);
                    }
                    formData.append("image_file", imageFileInput.files[0]);
                }
                const response = await fetch("/api/user/self-test/run", {
                    method: "POST",
                    body: formData,
                    credentials: "same-origin",
                });
                const text = await response.text();
                const data = safeJsonParse(text) ?? {};
                renderUserSelfTestScenarios(scenarioTableBody, data.scenarios || []);
                if (!response.ok || data.success === false) {
                    const resolution = deriveUserSelfTestResolution(data);
                    const summaryValue = summarizeUserSelfTestScenarios(data);
                    summary.innerHTML = `
                        <div><span>状态</span><strong>失败</strong></div>
                        <div><span>模型</span><strong>${escapeHtml(data.model_name || modelSelect.value)}</strong></div>
                        <div><span>成功 / 失败</span><strong>${summaryValue.success} / ${summaryValue.failed}</strong></div>
                        <div><span>代表渠道</span><strong>${escapeHtml(data.provider_name || "-")}</strong></div>
                    `;
                    renderResolution(failure, resolution.failure);
                    renderResolution(fixes, resolution.fixes);
                    renderResolution(next, resolution.next);
                    output.textContent = formatCodeValue(data.output_text || data.response_preview || data.message || data.detail || "测试失败");
                    trace.textContent = formatCodeValue(data.trace || data.message || data.detail || data);
                    lastSelfTestResult = data;
                    downloadBtn.disabled = false;
                    showToast(data.message || data.detail || "测试失败", "error");
                    return;
                }
                const resolution = deriveUserSelfTestResolution(data);
                const summaryValue = summarizeUserSelfTestScenarios(data);
                summary.innerHTML = `
                    <div><span>状态</span><strong>${data.success ? "成功" : "失败"}</strong></div>
                    <div><span>模型</span><strong>${escapeHtml(data.model_name || modelSelect.value)}</strong></div>
                    <div><span>场景通过</span><strong>${summaryValue.success} / ${summaryValue.total}</strong></div>
                    <div><span>图片链路</span><strong>${summaryValue.imageEnabled ? "已覆盖" : "未覆盖"}</strong></div>
                `;
                renderResolution(failure, resolution.failure);
                renderResolution(fixes, resolution.fixes);
                renderResolution(next, resolution.next);
                output.textContent = formatCodeValue(data.output_text || data.response_preview || data.message);
                trace.textContent = formatCodeValue(data.trace || data.message || data);
                lastSelfTestResult = data;
                downloadBtn.disabled = false;
                showToast(data.success ? "真实测试完成" : "测试返回失败结果", data.success ? "success" : "error");
            } catch (error) {
                const resolution = deriveUserSelfTestResolution({ success: false, message: error.message });
                summary.innerHTML = `<div><span>状态</span><strong>失败</strong></div><div><span>原因</span><strong>${escapeHtml(error.message)}</strong></div>`;
                renderResolution(failure, resolution.failure);
                renderResolution(fixes, resolution.fixes);
                renderResolution(next, resolution.next);
                output.textContent = error.message;
                trace.textContent = error.message;
                renderUserSelfTestScenarios(scenarioTableBody, []);
                lastSelfTestResult = null;
                resetDownloadButton();
                showToast(error.message, "error");
            } finally {
                setButtonLoading(runBtn, false);
            }
        });
    }

    async function initAlertsPage() {
        const form = document.getElementById("alerts-subscription-form");
        const refreshBtn = document.getElementById("alerts-feed-refresh-btn");
        const tableBody = document.getElementById("alerts-event-table-body");
        const systemMetricRoot = document.getElementById("alerts-system-metrics");
        if (!form || !refreshBtn || !tableBody) {
            return;
        }
        const notifiedKeys = new Set();
        let pollTimer = null;

        function formatAlertTypeLabel(type) {
            const labels = {
                provider: "渠道异常",
                api_key: "API 密钥异常",
                account: "账户预警",
                failure_rate: "失败率异常",
            };
            return labels[type] || type || "-";
        }

        function formatAlertSeverityLabel(severity) {
            const labels = {
                danger: "严重",
                warning: "警告",
                info: "提示",
            };
            return labels[severity] || severity || "-";
        }

        function renderAlertSuggestion(type) {
            if (type === "provider") return "先检查中转站健康、熔断和最近延迟，再决定是否临时下线。";
            if (type === "api_key") return "优先核对密钥状态、余额和授权渠道，再决定是否轮换或恢复。";
            if (type === "account") return "先确认账户额度、冻结金额和最近消费，再决定调账或提升配额。";
            return "结合失败率、消息内容和相关日志，优先处理影响正式流量的问题。";
        }

        function renderSystemMetrics(metrics) {
            if (!systemMetricRoot || !metrics) return;
            const values = {
                status: metrics.status || "-",
                redis_status: metrics.redis?.status || "-",
                database_status: metrics.database?.status || "-",
                active_requests: metrics.redis?.active_requests ?? "-",
                active_streams: metrics.redis?.active_streams ?? "-",
                pending_finalize_logs: metrics.background?.pending_finalize_logs ?? "-",
                status_5xx: metrics.traffic?.status_5xx ?? 0,
                status_429: metrics.traffic?.status_429 ?? 0,
            };
            Object.entries(values).forEach(([key, value]) => {
                const node = systemMetricRoot.querySelector(`[data-alert-metric="${key}"]`);
                if (node) node.textContent = String(value);
            });
        }

        function renderAlertActions(item) {
            const actionLinks = {
                provider: '<a class="table-action-btn" href="/providers" data-shell-link>查渠道</a>',
                api_key: '<a class="table-action-btn" href="/api-keys" data-shell-link>查密钥</a>',
                account: '<a class="table-action-btn" href="/users" data-shell-link>查账户</a>',
            };
            return `
                <div class="table-actions">
                    <button class="table-action-btn" type="button" data-alert-ack-id="${escapeHtml(String(item.id))}">确认</button>
                    ${actionLinks[item.alert_type] || ""}
                </div>
            `;
        }

        function renderEvents(events = []) {
            tableBody.innerHTML = events.length
                ? events.map((item) => `
                    <tr>
                        <td>${escapeHtml(formatAlertTypeLabel(item.alert_type))}</td>
                        <td>${escapeHtml(formatAlertSeverityLabel(item.severity))}</td>
                        <td>${escapeHtml(item.title || "-")}</td>
                        <td>${escapeHtml(renderAlertSuggestion(item.alert_type))}</td>
                        <td>${escapeHtml(item.last_seen_at || "-")}</td>
                        <td>${renderAlertActions(item)}</td>
                    </tr>
                `).join("")
                : '<tr><td colspan="6"><div class="empty-state">当前没有活跃告警事件。</div></td></tr>';
            enhanceInteractiveButtons(tableBody);
        }

        async function maybeNotify(events, subscription) {
            if (!subscription?.enabled || !subscription.browser_notifications_enabled || !("Notification" in window)) {
                return;
            }
            if (Notification.permission === "default") {
                await Notification.requestPermission();
            }
            if (Notification.permission !== "granted") {
                return;
            }
            events.forEach((item) => {
                if (notifiedKeys.has(item.alert_key)) return;
                notifiedKeys.add(item.alert_key);
                new Notification(item.title || "新告警", { body: item.message || "" });
            });
        }

        async function loadFeed(manual = false) {
            try {
                if (manual) setButtonLoading(refreshBtn, true);
                const data = await api.get("/api/alerts/feed");
                renderEvents(data.events || []);
                renderSystemMetrics(data.system_metrics);
                await maybeNotify(data.events || [], data.subscription || {});
                if (manual) showToast("告警事件已刷新");
                if (pollTimer) window.clearTimeout(pollTimer);
                const nextPoll = Math.max(10, Number(data.subscription?.poll_interval_seconds || 30)) * 1000;
                pollTimer = window.setTimeout(() => {
                    loadFeed().catch(() => undefined);
                }, nextPoll);
            } catch (error) {
                if (manual) showToast(error.message, "error");
            } finally {
                if (manual) setButtonLoading(refreshBtn, false);
            }
        }

        form.addEventListener("submit", async (event) => {
            event.preventDefault();
            try {
                const formData = new FormData();
                formData.append("enabled", document.getElementById("alerts-subscription-enabled").checked ? "true" : "false");
                formData.append("notify_provider_alerts", document.getElementById("alerts-subscription-provider").checked ? "true" : "false");
                formData.append("notify_api_key_alerts", document.getElementById("alerts-subscription-api-key").checked ? "true" : "false");
                formData.append("notify_account_alerts", document.getElementById("alerts-subscription-account").checked ? "true" : "false");
                formData.append("notify_failure_rate_alerts", document.getElementById("alerts-subscription-failure-rate").checked ? "true" : "false");
                formData.append("browser_notifications_enabled", document.getElementById("alerts-subscription-browser").checked ? "true" : "false");
                formData.append("poll_interval_seconds", document.getElementById("alerts-subscription-poll").value || "30");
                const response = await fetch("/api/alerts/subscription", { method: "POST", body: formData, credentials: "same-origin" });
                if (!response.ok) {
                    const text = await response.text();
                    throw new Error(text || "保存订阅失败");
                }
                showToast("告警订阅已保存");
                await loadFeed();
            } catch (error) {
                showToast(error.message, "error");
            }
        });

        refreshBtn.addEventListener("click", async () => {
            await loadFeed(true);
        });

        tableBody.addEventListener("click", async (event) => {
            const button = event.target.closest("[data-alert-ack-id]");
            if (!button) return;
            try {
                const response = await fetch(`/api/alerts/${button.dataset.alertAckId}/ack`, { method: "POST", credentials: "same-origin" });
                if (!response.ok) {
                    const text = await response.text();
                    throw new Error(text || "确认失败");
                }
                showToast("告警已确认");
                await loadFeed();
            } catch (error) {
                showToast(error.message, "error");
            }
        });

        await loadFeed();
    }

    async function initUsersPage() {
        const checkAll = document.getElementById("users-check-all-visible");
        const hiddenInput = document.getElementById("users-batch-user-ids");
        const previewInput = document.getElementById("users-batch-user-ids-preview");
        const form = document.getElementById("users-batch-quota-form");
        const submitBtn = document.getElementById("users-batch-apply-btn");
        const quotaFieldNodes = Array.from(document.querySelectorAll("[data-batch-quota-field]"));
        const quotaToggleNodes = Array.from(document.querySelectorAll("[data-batch-quota-toggle]"));
        const quotaInputNodes = Array.from(document.querySelectorAll("[data-batch-quota-input]"));
        const quotaCurrentNodes = Array.from(document.querySelectorAll("[data-batch-quota-current]"));
        if (!checkAll || !hiddenInput || !previewInput || !form) {
            return;
        }
        const quotaInputsByField = new Map(quotaInputNodes.map((node) => [node.dataset.batchQuotaInput, node]));
        const quotaCurrentByField = new Map(quotaCurrentNodes.map((node) => [node.dataset.batchQuotaCurrent, node]));
        const quotaTogglesByField = new Map(quotaToggleNodes.map((node) => [node.dataset.batchQuotaToggle, node]));

        const syncQuotaFieldState = () => {
            quotaFieldNodes.forEach((fieldNode) => {
                const fieldName = fieldNode.dataset.batchQuotaField;
                const toggle = quotaTogglesByField.get(fieldName);
                const input = quotaInputsByField.get(fieldName);
                const enabled = Boolean(toggle?.checked);
                fieldNode.classList.toggle("is-enabled", enabled);
                if (input) {
                    input.disabled = !enabled;
                }
            });
        };

        const resetQuotaPreview = () => {
            quotaCurrentByField.forEach((node) => {
                node.textContent = "当前值：未选择用户";
            });
            quotaInputsByField.forEach((node) => {
                node.placeholder = "";
            });
        };

        const refreshQuotaPreview = async () => {
            const selectedIds = hiddenInput.value
                .split(",")
                .map((item) => item.trim())
                .filter(Boolean);
            if (!selectedIds.length) {
                resetQuotaPreview();
                return;
            }
            try {
                const query = selectedIds.map((id) => `user_ids=${encodeURIComponent(id)}`).join("&");
                const data = await api.get(`/api/users/quota-preview?${query}`);
                Object.entries(data?.fields || {}).forEach(([fieldName, item]) => {
                    const currentNode = quotaCurrentByField.get(fieldName);
                    const input = quotaInputsByField.get(fieldName);
                    if (currentNode) {
                        currentNode.textContent = `当前值：${item.display_value || "未知"}`;
                    }
                    if (input && !input.value) {
                        input.placeholder = item.raw_value == null ? "" : String(item.raw_value);
                    }
                });
            } catch (error) {
                console.error("批量额度原值预览失败", error);
                showToast(error.message || "批量额度原值预览失败", "error");
            }
        };

        const updateSelection = () => {
            const selectedIds = Array.from(document.querySelectorAll("[data-user-batch-id]:checked"))
                .map((node) => node.dataset.userBatchId)
                .filter(Boolean);
            hiddenInput.value = selectedIds.join(",");
            previewInput.value = selectedIds.join(", ");
            const checkboxes = Array.from(document.querySelectorAll("[data-user-batch-id]"));
            checkAll.checked = checkboxes.length > 0 && selectedIds.length === checkboxes.length;
            checkAll.indeterminate = selectedIds.length > 0 && selectedIds.length < checkboxes.length;
        };
        const updateSelectionAndPreview = async () => {
            updateSelection();
            await refreshQuotaPreview();
        };
        checkAll.addEventListener("change", () => {
            document.querySelectorAll("[data-user-batch-id]").forEach((node) => {
                node.checked = checkAll.checked;
            });
            updateSelectionAndPreview();
        });
        quotaToggleNodes.forEach((node) => {
            node.addEventListener("change", syncQuotaFieldState);
        });
        document.querySelectorAll("[data-user-batch-id]").forEach((node) => {
            node.addEventListener("change", updateSelectionAndPreview);
        });
        form.addEventListener("submit", async (event) => {
            updateSelection();
            if (!hiddenInput.value) {
                event.preventDefault();
                showToast("请先选择至少一个用户", "error");
                console.error("批量应用额度失败：未选择用户");
                return;
            }
            if (!quotaToggleNodes.some((node) => node.checked)) {
                event.preventDefault();
                showToast("请至少勾选一个要修改的额度项", "error");
                console.error("批量应用额度失败：未勾选额度项");
                return;
            }
            event.preventDefault();
            const request = new Request(form.action, {
                method: "POST",
                body: new FormData(form),
                credentials: "same-origin",
                headers: {
                    Accept: "application/json",
                    "X-Requested-With": "XMLHttpRequest",
                },
            });
            try {
                if (submitBtn) setButtonLoading(submitBtn, true);
                const data = await parseResponse(await fetch(request));
                const message = data?.message || "已批量应用用户额度";
                console.info(message, data);
                showToast(message);
                await refreshQuotaPreview();
            } catch (error) {
                const message = error?.message || "批量应用额度失败";
                console.error(message, error);
                showToast(message, "error");
            } finally {
                if (submitBtn) setButtonLoading(submitBtn, false);
            }
        });
        syncQuotaFieldState();
        updateSelection();
        await refreshQuotaPreview();
    }

    function renderApiKeyStatusText(status) {
        return formatApiKeyStatusLabel(status);
    }

    function buildApiKeyQuotaSummary(apiKey) {
        const usedTokens = Number(apiKey.total_tokens_used || 0);
        if (apiKey.token_limit_total == null) {
            return {
                percent: 100,
                summary: `无限额 · 已累计 ${formatNumber(usedTokens)} tokens`,
            };
        }
        const limit = Math.max(Number(apiKey.token_limit_total || 0), 0);
        const percent = limit > 0 ? Math.min(100, Math.round((usedTokens / limit) * 100)) : 0;
        return {
            percent,
            summary: `${formatNumber(usedTokens)} / ${formatNumber(limit)} tokens`,
        };
    }

    function buildApiKeyCostSummary(apiKey) {
        const usedCost = Number(apiKey.total_cost_used || 0);
        const balance = apiKey.balance_amount == null ? null : Number(apiKey.balance_amount || 0);
        const costLimit = apiKey.cost_limit_total == null ? null : Number(apiKey.cost_limit_total || 0);
        return {
            summary: `累计消费 ${formatMoney(usedCost)}`,
            detail: `余额 ${balance == null ? "不限" : formatMoney(balance)} · 金额额度 ${costLimit == null ? "不限" : formatMoney(costLimit)}`,
        };
    }

    function renderApiKeyProviderSelector(container, providers = [], selectedIds = []) {
        if (!container) return;
        if (!providers.length) {
            container.innerHTML = '<div class="playground-provider-list-empty">当前还没有可绑定的中转站</div>';
            return;
        }
        const selectedIdSet = new Set(selectedIds.map((item) => Number(item)));
        container.innerHTML = providers.map((provider) => `
            <label class="playground-provider-option api-key-provider-option">
                <input type="checkbox" data-api-key-provider-id="${provider.id}" ${selectedIdSet.has(provider.id) ? "checked" : ""}>
                <div class="playground-provider-option-copy">
                    <strong>${escapeHtml(provider.name)}</strong>
                    <span>${escapeHtml(provider.models.join(", ") || "未配置模型")}</span>
                </div>
                <div>${statusBadge(provider.health_status || "unknown")}</div>
            </label>
        `).join("");
    }

    async function initApiKeys() {
        const summarySignal = document.getElementById("api-key-summary-signal");
        const summarySignalSlots = document.querySelectorAll("[data-api-key-summary-signal]");
        const tableBody = document.getElementById("api-key-table-body");
        const searchInput = document.getElementById("api-key-search");
        const statusFilter = document.getElementById("api-key-status-filter");
        const enabledFilter = document.getElementById("api-key-enabled-filter");
        const ownerFilter = document.getElementById("api-key-owner-filter");
        const pageSizeSelect = document.getElementById("api-key-page-size");
        const refreshBtn = document.getElementById("api-key-refresh-btn");
        const pageMeta = document.getElementById("api-key-page-meta");
        const prevPageBtn = document.getElementById("api-key-prev-page-btn");
        const nextPageBtn = document.getElementById("api-key-next-page-btn");
        const selectPageBtn = document.getElementById("api-key-select-page");
        const clearSelectionBtn = document.getElementById("api-key-clear-selection-btn");
        const batchEnableBtn = document.getElementById("api-key-batch-enable-btn");
        const batchDisableBtn = document.getElementById("api-key-batch-disable-btn");
        const batchDeleteBtn = document.getElementById("api-key-batch-delete-btn");
        const batchRotateBtn = document.getElementById("api-key-batch-rotate-btn");
        const batchExpireBtn = document.getElementById("api-key-batch-expire-btn");
        const batchTemplateBtn = document.getElementById("api-key-batch-template-btn");
        const batchProvidersBtn = document.getElementById("api-key-batch-providers-btn");
        const checkAllVisibleInput = document.getElementById("api-key-check-all-visible");
        const addBtn = document.getElementById("add-api-key-btn");
        const modal = document.getElementById("api-key-modal");
        const modalTitle = document.getElementById("api-key-modal-title");
        const closeBtn = document.getElementById("api-key-modal-close");
        const cancelBtn = document.getElementById("api-key-form-cancel");
        const submitBtn = document.getElementById("api-key-submit-btn");
        const rawPanel = document.getElementById("api-key-raw-panel");
        const rawValue = document.getElementById("api-key-raw-value");
        const copyRawBtn = document.getElementById("api-key-copy-raw-btn");
        const providerSelector = document.getElementById("api-key-provider-selector");
        const modelSelector = document.getElementById("api-key-model-selector");
        const defaultProviderSelect = document.getElementById("api-key-default-provider-id");
        const routeModeInput = document.getElementById("api-key-route-mode");
        const manualFallbackInput = document.getElementById("api-key-manual-allow-fallback");
        const enabledInput = document.getElementById("api-key-enabled");
        const ownerUserSelect = document.getElementById("api-key-owner-user-id");
        const expiresAtInput = document.getElementById("api-key-expires-at");
        const tokenLimitInput = document.getElementById("api-key-token-limit-total");
        const costLimitInput = document.getElementById("api-key-cost-limit-total");
        const balanceAmountInput = document.getElementById("api-key-balance-amount");
        const requestLimitDailyInput = document.getElementById("api-key-request-limit-daily");
        const qpsLimitInput = document.getElementById("api-key-qps-limit");
        const rpmLimitInput = document.getElementById("api-key-rpm-limit");
        const tpmLimitInput = document.getElementById("api-key-tpm-limit");
        const nameInput = document.getElementById("api-key-name");
        const generationModeInput = document.getElementById("api-key-generation-mode");
        const rawApiKeyInput = document.getElementById("api-key-raw-api-key");
        const rawApiKeyFeedback = document.getElementById("api-key-raw-api-key-feedback");
        const remarkInput = document.getElementById("api-key-remark");
        const idInput = document.getElementById("api-key-id");
        const form = document.getElementById("api-key-form");
        const templateSelect = document.getElementById("api-key-template-id");
        const templateAddBtn = document.getElementById("api-key-template-add-btn");
        const templateTableBody = document.getElementById("api-key-template-table-body");
        const templateModal = document.getElementById("api-key-template-modal");
        const templateModalTitle = document.getElementById("api-key-template-modal-title");
        const templateModalCloseBtn = document.getElementById("api-key-template-modal-close");
        const templateModalCancelBtn = document.getElementById("api-key-template-cancel");
        const templateForm = document.getElementById("api-key-template-form");
        const templateSubmitBtn = document.getElementById("api-key-template-submit-btn");
        const templateEditIdInput = document.getElementById("api-key-template-edit-id");
        const templateNameInput = document.getElementById("api-key-template-name");
        const templateRouteModeInput = document.getElementById("api-key-template-route-mode");
        const templateDefaultProviderSelect = document.getElementById("api-key-template-default-provider-id");
        const templateTokenLimitInput = document.getElementById("api-key-template-token-limit-total");
        const templateCostLimitInput = document.getElementById("api-key-template-cost-limit-total");
        const templateExpiresInDaysInput = document.getElementById("api-key-template-expires-in-days");
        const templateEnabledInput = document.getElementById("api-key-template-enabled");
        const templateManualFallbackInput = document.getElementById("api-key-template-manual-allow-fallback");
        const templateRemarkInput = document.getElementById("api-key-template-remark");
        const templateProviderSelector = document.getElementById("api-key-template-provider-selector");
        const templateModelSelector = document.getElementById("api-key-template-model-selector");
        const batchTemplateModal = document.getElementById("api-key-batch-template-modal");
        const batchTemplateCloseBtn = document.getElementById("api-key-batch-template-close");
        const batchTemplateCancelBtn = document.getElementById("api-key-batch-template-cancel");
        const batchTemplateForm = document.getElementById("api-key-batch-template-form");
        const batchTemplateSelect = document.getElementById("api-key-batch-template-id");
        const batchTemplateSubmitBtn = document.getElementById("api-key-batch-template-submit");
        const batchRotateModal = document.getElementById("api-key-batch-rotate-modal");
        const batchRotateCloseBtn = document.getElementById("api-key-batch-rotate-close");
        const batchRotateCancelBtn = document.getElementById("api-key-batch-rotate-cancel");
        const batchRotateCopyBtn = document.getElementById("api-key-batch-rotate-copy");
        const batchRotateResult = document.getElementById("api-key-batch-rotate-result");
        const insightGroupBySelect = document.getElementById("api-key-insight-group-by");
        const insightWindowDaysSelect = document.getElementById("api-key-insight-window-days");
        const insightRefreshBtn = document.getElementById("api-key-insight-refresh-btn");
        const insightTableBody = document.getElementById("api-key-insight-table-body");
        const batchProviderModal = document.getElementById("api-key-batch-provider-modal");
        const batchProviderCloseBtn = document.getElementById("api-key-batch-provider-close");
        const batchProviderCancelBtn = document.getElementById("api-key-batch-provider-cancel");
        const batchProviderForm = document.getElementById("api-key-batch-provider-form");
        const batchProviderRouteMode = document.getElementById("api-key-batch-provider-route-mode");
        const batchProviderDefault = document.getElementById("api-key-batch-provider-default");
        const batchProviderFallback = document.getElementById("api-key-batch-provider-fallback");
        const batchProviderSelector = document.getElementById("api-key-batch-provider-selector");
        const batchProviderSubmitBtn = document.getElementById("api-key-batch-provider-submit");
        const state = {
            summary: null,
            apiKeys: [],
            providers: [],
            models: [],
            users: [],
            templates: [],
            total: 0,
            page: 1,
            pageSize: Number.parseInt(pageSizeSelect.value || "20", 10) || 20,
            filters: {
                keyword: "",
                status: "",
                enabled: "",
                ownerUserId: "",
            },
            selectedIds: new Set(),
        };
        let searchTimer = null;

        function formatLimitSetting(value) {
            return value == null ? "不限" : formatNumber(value);
        }

        function renderSummary(summary) {
            document.querySelectorAll("[data-api-key-summary]").forEach((node) => {
                const key = node.dataset.apiKeySummary;
                if (["total_cost_used", "total_balance_amount", "total_recharge_amount"].includes(key)) {
                    node.textContent = formatMoney(summary?.[key] ?? 0);
                    return;
                }
                node.textContent = formatNumber(summary?.[key] ?? 0);
            });
            const total = Number(summary?.total_keys || 0);
            const enabled = Number(summary?.enabled_keys || 0);
            const disabled = Number(summary?.disabled_keys || 0);
            const activeRatio = total ? Math.round((enabled / total) * 100) : 0;
            const summaryMarkup = `
                <div class="cockpit-aside-label">密钥总览</div>
                <div class="cockpit-aside-value">${formatNumber(total)}</div>
                <div class="cockpit-aside-copy">当前可运维的 API 密钥总数</div>
                <div class="cockpit-health-bar"><span style="width:${activeRatio}%"></span></div>
                <div class="cockpit-aside-meta">
                    <span>启用 ${formatNumber(enabled)}</span>
                    <span>禁用 ${formatNumber(disabled)}</span>
                </div>
            `;
            if (summarySignalSlots.length) {
                summarySignalSlots.forEach((node) => {
                    node.innerHTML = summaryMarkup;
                });
            } else if (summarySignal) {
                summarySignal.innerHTML = summaryMarkup;
            }
        }

        function syncFilterStateFromInputs() {
            state.filters.keyword = searchInput.value.trim();
            state.filters.status = statusFilter.value;
            state.filters.enabled = enabledFilter.value;
            state.filters.ownerUserId = ownerFilter.value;
        }

        function populateOwnerFilterOptions(selectedUserId = ownerFilter.value) {
            ownerFilter.innerHTML = '<option value="">全部归属用户</option>' + state.users.map((user) => `
                <option value="${user.id}" ${String(selectedUserId || "") === String(user.id) ? "selected" : ""}>
                    ${escapeHtml(user.username)} · ${escapeHtml(user.email)}
                </option>
            `).join("");
        }

        function getVisibleApiKeyIds() {
            return state.apiKeys.map((item) => item.id);
        }

        function renderPagination() {
            const totalPages = Math.max(1, Math.ceil((state.total || 0) / state.pageSize));
            if (state.page > totalPages) {
                state.page = totalPages;
            }
            pageMeta.textContent = `第 ${formatNumber(state.page)} 页，共 ${formatNumber(totalPages)} 页 · 共 ${formatNumber(state.total)} 条 · 已选 ${formatNumber(state.selectedIds.size)} 条`;
            prevPageBtn.disabled = state.page <= 1;
            nextPageBtn.disabled = state.page >= totalPages;
        }

        function renderBatchButtons() {
            const hasSelection = state.selectedIds.size > 0;
            batchEnableBtn.disabled = !hasSelection;
            batchDisableBtn.disabled = !hasSelection;
            batchDeleteBtn.disabled = !hasSelection;
            batchRotateBtn.disabled = !hasSelection;
            batchExpireBtn.disabled = !hasSelection;
            batchTemplateBtn.disabled = !hasSelection;
            batchProvidersBtn.disabled = !hasSelection;
            clearSelectionBtn.disabled = !hasSelection;
            const visibleIds = getVisibleApiKeyIds();
            const visibleSelectedCount = visibleIds.filter((id) => state.selectedIds.has(id)).length;
            checkAllVisibleInput.checked = visibleIds.length > 0 && visibleSelectedCount === visibleIds.length;
            checkAllVisibleInput.indeterminate = visibleSelectedCount > 0 && visibleSelectedCount < visibleIds.length;
        }

        function renderTable() {
            tableBody.innerHTML = state.apiKeys.map((item) => {
                const quota = buildApiKeyQuotaSummary(item);
                const cost = buildApiKeyCostSummary(item);
                const defaultProvider = state.providers.find((provider) => provider.id === item.default_provider_id);
                return `
                    <tr>
                        <td>
                            <input type="checkbox" data-api-key-select="${item.id}" ${state.selectedIds.has(item.id) ? "checked" : ""} aria-label="选择 API Key ${escapeHtml(item.name)}">
                        </td>
                        <td>
                            <strong>${escapeHtml(item.name)}</strong>
                            <div class="table-muted">${escapeHtml(item.remark || "-")}</div>
                        </td>
                        <td>
                            <strong>${escapeHtml(item.owner_user_name || "未分配")}</strong>
                            <div class="table-muted">${item.owner_user_id ? `用户 ID ${escapeHtml(String(item.owner_user_id))}` : "管理员侧未绑定用户"}</div>
                        </td>
                        <td>
                            <strong>${escapeHtml(item.raw_api_key || item.key_masked)}</strong>
                            <div class="table-muted">${escapeHtml(item.raw_api_key ? item.key_prefix : "历史密钥未存明文，可编辑后重置")}</div>
                            <div class="hero-actions">
                                <button class="table-action-btn" type="button" ${item.raw_api_key ? `data-copy-text="${escapeHtml(item.raw_api_key)}"` : "disabled"}>${item.raw_api_key ? "复制密钥" : "不可复制"}</button>
                            </div>
                        </td>
                        <td>
                            ${statusBadge(item.status)}
                            <div class="table-muted">${escapeHtml(renderApiKeyStatusText(item.status))}</div>
                        </td>
                        <td>
                            <strong>${escapeHtml(formatRouteModeLabel(item.route_mode))}</strong>
                            <div class="table-muted">默认中转 ${escapeHtml(defaultProvider?.name || (item.default_provider_id ? String(item.default_provider_id) : "-"))} · 失败后回退 ${formatSwitchText(item.manual_allow_fallback)}</div>
                        </td>
                        <td>
                            <strong>${formatNumber(item.allowed_provider_ids.length)} 个</strong>
                            <div class="table-muted">${escapeHtml(item.allowed_providers.map((provider) => provider.name).join(", ") || "未绑定")}</div>
                            <div class="table-muted">模型 ${escapeHtml((item.allowed_model_names || []).length ? `${formatNumber(item.allowed_model_names.length)} 个白名单` : "全部可路由")}</div>
                        </td>
                        <td>
                            <strong>${escapeHtml(quota.summary)}</strong>
                            <div class="table-muted">剩余 ${item.remaining_tokens == null ? "无限额" : formatNumber(item.remaining_tokens)}</div>
                            <div class="table-muted">频控 QPS ${formatLimitSetting(item.qps_limit)} · RPM ${formatLimitSetting(item.rpm_limit)} · TPM ${formatLimitSetting(item.tpm_limit)}</div>
                        </td>
                        <td>
                            <strong>${escapeHtml(cost.summary)}</strong>
                            <div class="table-muted">${escapeHtml(cost.detail)}</div>
                        </td>
                        <td>
                            <strong>${formatDate(item.last_used_at)}</strong>
                            <div class="table-muted">过期 ${formatDate(item.expires_at)}</div>
                        </td>
                        <td>
                            <div class="table-actions">
                                <button class="table-action-btn" data-action="detail" data-api-key-id="${item.id}">详情</button>
                                <button class="table-action-btn" data-action="edit" data-api-key-id="${item.id}">编辑</button>
                                <button class="table-action-btn" data-action="${item.enabled ? "disable" : "enable"}" data-api-key-id="${item.id}">${item.enabled ? "禁用" : "启用"}</button>
                                <button class="table-action-btn" data-action="delete" data-api-key-id="${item.id}">删除</button>
                            </div>
                        </td>
                    </tr>
                `;
            }).join("") || '<tr><td colspan="11"><div class="empty-state">当前筛选条件下暂无 API 密钥</div></td></tr>';
            enhanceInteractiveButtons(tableBody);
            renderPagination();
            renderBatchButtons();
        }

        function refreshRawApiKeyInputState() {
            const isEditing = Boolean(idInput.value);
            if (isEditing) {
                generationModeInput.disabled = true;
                rawApiKeyInput.disabled = false;
                rawApiKeyInput.placeholder = "留空表示保持当前密钥不变；填写则替换为新密钥";
                const result = updateRawApiKeyValidationState(rawApiKeyInput, rawApiKeyFeedback, { allowEmpty: true });
                submitBtn.disabled = !result.valid;
                return;
            }
            generationModeInput.disabled = false;
            const isCustom = generationModeInput.value === "custom";
            rawApiKeyInput.disabled = !isCustom;
            rawApiKeyInput.placeholder = isCustom
                ? "请输入自定义密钥，必须以 sk-aotu- 开头，长度 24-128 位"
                : "自动生成时留空";
            if (!isCustom) {
                rawApiKeyInput.value = "";
            }
            const result = updateRawApiKeyValidationState(rawApiKeyInput, rawApiKeyFeedback, { allowEmpty: !isCustom });
            submitBtn.disabled = isCustom && !result.valid;
        }

        function populateDefaultProviderOptions(selectedProviderId = null) {
            defaultProviderSelect.innerHTML = '<option value="">未设置</option>' + state.providers.map((provider) => `
                <option value="${provider.id}" ${Number(selectedProviderId) === provider.id ? "selected" : ""}>
                    ${escapeHtml(provider.name)} ${provider.enabled ? "" : "(已禁用)"}
                </option>
            `).join("");
        }

        function populateOwnerUserOptions(selectedUserId = null) {
            ownerUserSelect.innerHTML = '<option value="">未分配</option>' + state.users.map((user) => `
                <option value="${user.id}" ${Number(selectedUserId) === user.id ? "selected" : ""}>
                    ${escapeHtml(user.username)} · ${escapeHtml(user.email)}
                </option>
            `).join("");
        }

        function getSelectedProviderIds() {
            return Array.from(providerSelector.querySelectorAll("[data-api-key-provider-id]:checked"))
                .map((input) => Number(input.dataset.apiKeyProviderId))
                .filter((value) => Number.isFinite(value));
        }

        function renderApiKeyModelSelector(container, models = [], selectedNames = []) {
            if (!container) return;
            const selectedSet = new Set((selectedNames || []).map((item) => String(item)));
            if (!models.length) {
                container.innerHTML = '<div class="playground-provider-list-empty">当前模型目录为空，请先在模型聚合配置中维护模型。</div>';
                return;
            }
            container.innerHTML = models.map((model) => {
                const modelName = model.model_name || "";
                const title = model.display_name || modelName;
                const providerCount = (model.provider_bindings || []).filter((binding) => binding.bound && binding.enabled && binding.provider_enabled).length;
                return `
                    <label class="playground-provider-option api-key-provider-option">
                        <input type="checkbox" data-api-key-model-name="${escapeHtml(modelName)}" ${selectedSet.has(modelName) ? "checked" : ""}>
                        <div class="playground-provider-option-copy">
                            <strong>${escapeHtml(title)}</strong>
                            <span>${escapeHtml(modelName)} · ${model.enabled ? "已启用" : "已禁用"} · 可用渠道 ${formatNumber(providerCount)}</span>
                        </div>
                        <div>${statusBadge(model.enabled ? "healthy" : "disabled")}</div>
                    </label>
                `;
            }).join("");
        }

        function getSelectedModelNames() {
            if (!modelSelector) return [];
            return Array.from(modelSelector.querySelectorAll("[data-api-key-model-name]:checked"))
                .map((input) => input.dataset.apiKeyModelName)
                .filter(Boolean);
        }

        function getBatchSelectedProviderIds() {
            return Array.from(batchProviderSelector.querySelectorAll("[data-api-key-provider-id]:checked"))
                .map((input) => Number(input.dataset.apiKeyProviderId))
                .filter((value) => Number.isFinite(value));
        }

        function getTemplateSelectedProviderIds() {
            return Array.from(templateProviderSelector.querySelectorAll("[data-api-key-provider-id]:checked"))
                .map((input) => Number(input.dataset.apiKeyProviderId))
                .filter((value) => Number.isFinite(value));
        }

        function getTemplateSelectedModelNames() {
            if (!templateModelSelector) return [];
            return Array.from(templateModelSelector.querySelectorAll("[data-api-key-model-name]:checked"))
                .map((input) => input.dataset.apiKeyModelName)
                .filter(Boolean);
        }

        function populateTemplateSelectOptions(selectedTemplateId = templateSelect?.value || "") {
            if (!templateSelect) return;
            const enabledTemplates = state.templates.filter((item) => item.enabled);
            templateSelect.innerHTML = '<option value="">不套用模板</option>' + enabledTemplates.map((item) => `
                <option value="${item.id}" ${String(item.id) === String(selectedTemplateId) ? "selected" : ""}>${escapeHtml(item.name)}</option>
            `).join("");
        }

        function populateBatchTemplateOptions(selectedTemplateId = batchTemplateSelect?.value || "") {
            if (!batchTemplateSelect) return;
            const enabledTemplates = state.templates.filter((item) => item.enabled);
            batchTemplateSelect.innerHTML = '<option value="">请选择一个模板</option>' + enabledTemplates.map((item) => `
                <option value="${item.id}" ${String(item.id) === String(selectedTemplateId) ? "selected" : ""}>${escapeHtml(item.name)}</option>
            `).join("");
        }

        function populateTemplateDefaultProviderOptions(selectedProviderId = null) {
            if (!templateDefaultProviderSelect) return;
            templateDefaultProviderSelect.innerHTML = '<option value="">未设置</option>' + state.providers.map((provider) => `
                <option value="${provider.id}" ${Number(selectedProviderId) === provider.id ? "selected" : ""}>
                    ${escapeHtml(provider.name)} ${provider.enabled ? "" : "(已禁用)"}
                </option>
            `).join("");
        }

        function applyTemplateToForm(templateId) {
            const template = state.templates.find((item) => String(item.id) === String(templateId));
            if (!template) return;
            routeModeInput.value = template.route_mode || "failover";
            manualFallbackInput.checked = template.manual_allow_fallback ?? true;
            enabledInput.checked = template.enabled ?? true;
            tokenLimitInput.value = template.token_limit_total ?? "";
            costLimitInput.value = template.cost_limit_total ?? "";
            expiresAtInput.value = template.expires_in_days ? toDatetimeLocalInputValue(new Date(Date.now() + (Number(template.expires_in_days) * 86400000)).toISOString()) : "";
            populateDefaultProviderOptions(template.default_provider_id || null);
            renderApiKeyProviderSelector(providerSelector, state.providers, template.allowed_provider_ids || []);
            renderApiKeyModelSelector(modelSelector, state.models, template.allowed_model_names || []);
            refreshRoutePreview();
        }

        function renderTemplateTable() {
            if (!templateTableBody) return;
            templateTableBody.innerHTML = state.templates.map((item) => {
                const defaultProvider = state.providers.find((provider) => provider.id === item.default_provider_id);
                return `
                    <tr>
                        <td>
                            <strong>${escapeHtml(item.name)}</strong>
                            <div class="table-muted">${escapeHtml(item.remark || "-")}</div>
                        </td>
                        <td>
                            <strong>${escapeHtml(formatRouteModeLabel(item.route_mode))}</strong>
                            <div class="table-muted">默认中转 ${escapeHtml(defaultProvider?.name || (item.default_provider_id ? String(item.default_provider_id) : "-"))} · 回退 ${formatSwitchText(item.manual_allow_fallback)}</div>
                        </td>
                        <td>
                            <strong>${formatNumber((item.allowed_provider_ids || []).length)} 个</strong>
                            <div class="table-muted">${escapeHtml((item.allowed_provider_ids || []).map((providerId) => state.providers.find((provider) => provider.id === providerId)?.name || String(providerId)).join(", ") || "未绑定")}</div>
                        </td>
                        <td>
                            <strong>${(item.allowed_model_names || []).length ? `${formatNumber(item.allowed_model_names.length)} 个` : "不限制"}</strong>
                            <div class="table-muted">${escapeHtml((item.allowed_model_names || []).join(", ") || "全部可路由模型")}</div>
                        </td>
                        <td>
                            <strong>Token ${item.token_limit_total == null ? "不限" : formatNumber(item.token_limit_total)}</strong>
                            <div class="table-muted">金额 ${item.cost_limit_total == null ? "不限" : formatMoney(item.cost_limit_total)}</div>
                        </td>
                        <td>
                            <strong>${item.expires_in_days == null ? "不自动过期" : `${formatNumber(item.expires_in_days)} 天后过期`}</strong>
                        </td>
                        <td>${statusBadge(item.enabled ? "healthy" : "disabled")}</td>
                        <td>
                            <div class="table-actions">
                                <button class="table-action-btn" data-action="edit-template" data-template-id="${item.id}">编辑</button>
                                <button class="table-action-btn" data-action="delete-template" data-template-id="${item.id}">删除</button>
                            </div>
                        </td>
                    </tr>
                `;
            }).join("") || '<tr><td colspan="8"><div class="empty-state">当前还没有策略模板</div></td></tr>';
            enhanceInteractiveButtons(templateTableBody);
        }

        function openTemplateModal(template = null) {
            if (!templateModal || !templateForm) return;
            templateModalTitle.textContent = template ? `编辑策略模板 #${template.id}` : "新增策略模板";
            templateEditIdInput.value = template?.id ?? "";
            templateNameInput.value = template?.name ?? "";
            templateRouteModeInput.value = template?.route_mode ?? "failover";
            templateTokenLimitInput.value = template?.token_limit_total ?? "";
            templateCostLimitInput.value = template?.cost_limit_total ?? "";
            templateExpiresInDaysInput.value = template?.expires_in_days ?? "";
            templateEnabledInput.checked = template?.enabled ?? true;
            templateManualFallbackInput.checked = template?.manual_allow_fallback ?? true;
            templateRemarkInput.value = template?.remark ?? "";
            populateTemplateDefaultProviderOptions(template?.default_provider_id || null);
            renderApiKeyProviderSelector(templateProviderSelector, state.providers, template?.allowed_provider_ids || []);
            renderApiKeyModelSelector(templateModelSelector, state.models, template?.allowed_model_names || []);
            templateModal.classList.remove("hidden");
        }

        function closeTemplateModal() {
            if (!templateModal || !templateForm) return;
            templateModal.classList.add("hidden");
            templateForm.reset();
            templateEditIdInput.value = "";
            renderApiKeyProviderSelector(templateProviderSelector, state.providers, []);
            renderApiKeyModelSelector(templateModelSelector, state.models, []);
            populateTemplateDefaultProviderOptions();
        }

        function openBatchTemplateModal() {
            populateBatchTemplateOptions();
            batchTemplateModal.classList.remove("hidden");
        }

        function closeBatchTemplateModal() {
            batchTemplateModal.classList.add("hidden");
            batchTemplateForm.reset();
            populateBatchTemplateOptions();
        }

        function openBatchRotateModal(result) {
            if (!batchRotateModal || !batchRotateResult) return;
            batchRotateResult.value = (result.items || []).map((item) => `${item.name}\t${item.key_masked}\t${item.raw_api_key}`).join("\n");
            batchRotateModal.classList.remove("hidden");
        }

        function closeBatchRotateModal() {
            if (!batchRotateModal || !batchRotateResult) return;
            batchRotateModal.classList.add("hidden");
            batchRotateResult.value = "";
        }

        function renderCostInsights(items = []) {
            if (!insightTableBody) return;
            insightTableBody.innerHTML = items.length ? items.map((item) => `
                <tr>
                    <td>${escapeHtml(item.dimension_value || "-")}</td>
                    <td>${formatNumber(item.total_requests || 0)}</td>
                    <td>${formatNumber(item.total_tokens || 0)}</td>
                    <td>${formatMoney(item.total_cost || 0)}</td>
                    <td>${item.avg_latency_ms ?? "-"} ms</td>
                </tr>
            `).join("") : '<tr><td colspan="5"><div class="empty-state">当前时间窗口内暂无成本透视数据。</div></td></tr>';
        }

        async function loadTemplates() {
            state.templates = await api.get("/api/api-key-policy-templates");
            populateTemplateSelectOptions();
            populateBatchTemplateOptions();
            renderTemplateTable();
        }

        async function loadCostInsights({ manual = false } = {}) {
            if (!insightGroupBySelect || !insightWindowDaysSelect) return;
            try {
                if (manual) setButtonLoading(insightRefreshBtn, true);
                const data = await api.get(`/api/api-keys/insights/cost?group_by=${encodeURIComponent(insightGroupBySelect.value)}&window_days=${encodeURIComponent(insightWindowDaysSelect.value)}`);
                renderCostInsights(Array.isArray(data.items) ? data.items : []);
                if (manual) showToast("成本透视已刷新");
            } catch (error) {
                renderCostInsights([]);
                if (manual) showToast(error.message, "error");
            } finally {
                if (manual) setButtonLoading(insightRefreshBtn, false);
            }
        }

        function refreshRoutePreview() {
            renderRoutePolicyGuideInto({
                modeCards: document.getElementById("api-key-route-mode-cards"),
                liveSummary: document.getElementById("api-key-route-live-summary"),
                routeMode: routeModeInput.value,
                defaultProviderId: defaultProviderSelect.value ? Number(defaultProviderSelect.value) : null,
                manualAllowFallback: manualFallbackInput.checked,
                providers: state.providers,
            });
        }

        function openModal(apiKey = null) {
            const isEditing = Boolean(apiKey);
            modalTitle.textContent = isEditing ? `编辑 API 密钥 #${apiKey.id}` : "新增 API 密钥";
            idInput.value = isEditing ? String(apiKey.id) : "";
            if (templateSelect) templateSelect.value = "";
            nameInput.value = apiKey?.name || "";
            generationModeInput.value = isEditing ? "custom" : "auto";
            rawApiKeyInput.value = isEditing ? (apiKey?.raw_api_key || "") : "";
            remarkInput.value = apiKey?.remark || "";
            routeModeInput.value = apiKey?.route_mode || "failover";
            enabledInput.checked = apiKey?.enabled ?? true;
            manualFallbackInput.checked = apiKey?.manual_allow_fallback ?? true;
            populateOwnerUserOptions(apiKey?.owner_user_id || null);
            expiresAtInput.value = toDatetimeLocalInputValue(apiKey?.expires_at);
            tokenLimitInput.value = apiKey?.token_limit_total ?? "";
            costLimitInput.value = apiKey?.cost_limit_total ?? "";
            balanceAmountInput.value = isEditing ? "" : (apiKey?.balance_amount ?? "");
            requestLimitDailyInput.value = apiKey?.request_limit_daily ?? "";
            qpsLimitInput.value = apiKey?.qps_limit ?? "";
            rpmLimitInput.value = apiKey?.rpm_limit ?? "";
            tpmLimitInput.value = apiKey?.tpm_limit ?? "";
            balanceAmountInput.disabled = isEditing;
            balanceAmountInput.placeholder = isEditing ? "已创建密钥请到详情页做余额调整" : "留空表示不限制";
            populateDefaultProviderOptions(apiKey?.default_provider_id || null);
            renderApiKeyProviderSelector(providerSelector, state.providers, apiKey?.allowed_provider_ids || []);
            renderApiKeyModelSelector(modelSelector, state.models, apiKey?.allowed_model_names || []);
            refreshRoutePreview();
            refreshRawApiKeyInputState();
            rawPanel.classList.add("hidden");
            rawValue.textContent = "";
            modal.classList.remove("hidden");
        }

        function closeModal() {
            modal.classList.add("hidden");
            rawPanel.classList.add("hidden");
            rawValue.textContent = "";
            form.reset();
            idInput.value = "";
            if (templateSelect) templateSelect.value = "";
            generationModeInput.value = "auto";
            rawApiKeyInput.value = "";
            balanceAmountInput.disabled = false;
            balanceAmountInput.placeholder = "留空表示不限制";
            renderApiKeyProviderSelector(providerSelector, state.providers, []);
            renderApiKeyModelSelector(modelSelector, state.models, []);
            populateDefaultProviderOptions();
            populateOwnerUserOptions();
            refreshRoutePreview();
            refreshRawApiKeyInputState();
        }

        function populateBatchProviderOptions(selectedProviderId = null) {
            batchProviderDefault.innerHTML = '<option value="">未设置</option>' + state.providers.map((provider) => `
                <option value="${provider.id}" ${Number(selectedProviderId) === provider.id ? "selected" : ""}>
                    ${escapeHtml(provider.name)} ${provider.enabled ? "" : "(已禁用)"}
                </option>
            `).join("");
        }

        function openBatchProviderModal() {
            populateBatchProviderOptions();
            renderApiKeyProviderSelector(batchProviderSelector, state.providers, []);
            batchProviderRouteMode.value = "failover";
            batchProviderFallback.checked = true;
            batchProviderModal.classList.remove("hidden");
        }

        function closeBatchProviderModal() {
            batchProviderModal.classList.add("hidden");
        }

        function renderLoadingState() {
            tableBody.innerHTML = '<tr><td colspan="11"><div class="empty-state">正在加载 API 密钥...</div></td></tr>';
            renderPagination();
            renderBatchButtons();
        }

        async function loadReferenceData() {
            const [providers, users, models] = await Promise.all([
                api.get("/api/providers"),
                api.get("/api/users/options"),
                api.get("/api/models"),
            ]);
            state.providers = providers;
            state.users = users;
            state.models = models;
            populateOwnerFilterOptions(state.filters.ownerUserId);
            populateDefaultProviderOptions(defaultProviderSelect.value ? Number(defaultProviderSelect.value) : null);
            populateTemplateDefaultProviderOptions(templateDefaultProviderSelect?.value ? Number(templateDefaultProviderSelect.value) : null);
            populateOwnerUserOptions(ownerUserSelect.value ? Number(ownerUserSelect.value) : null);
            renderApiKeyProviderSelector(providerSelector, state.providers, getSelectedProviderIds());
            renderApiKeyProviderSelector(batchProviderSelector, state.providers, getBatchSelectedProviderIds());
            renderApiKeyProviderSelector(templateProviderSelector, state.providers, getTemplateSelectedProviderIds());
            renderApiKeyModelSelector(modelSelector, state.models, getSelectedModelNames());
            renderApiKeyModelSelector(templateModelSelector, state.models, getTemplateSelectedModelNames());
            refreshRoutePreview();
        }

        async function loadTableData({ silent = false } = {}) {
            syncFilterStateFromInputs();
            const params = new URLSearchParams({
                page: String(state.page),
                page_size: String(state.pageSize),
            });
            if (state.filters.keyword) params.set("keyword", state.filters.keyword);
            if (state.filters.status) params.set("status", state.filters.status);
            if (state.filters.enabled) params.set("enabled", state.filters.enabled);
            if (state.filters.ownerUserId) params.set("owner_user_id", state.filters.ownerUserId);
            renderLoadingState();
            const [summary, result] = await Promise.all([
                api.get("/api/api-keys/summary"),
                api.get(`/api/api-keys/query?${params.toString()}`),
            ]);
            const totalPages = Math.max(1, Math.ceil((Number(result.total || 0)) / state.pageSize));
            if (Number(result.total || 0) > 0 && state.page > totalPages) {
                state.page = totalPages;
                return loadTableData({ silent: true });
            }
            state.summary = summary;
            state.apiKeys = result.items || [];
            state.total = Number(result.total || 0);
            renderSummary(summary);
            renderTable();
            if (!silent) showToast("API 密钥数据已刷新");
        }

        async function loadData({ silent = false, reloadReference = false } = {}) {
            if (reloadReference || !state.providers.length || !state.users.length || !state.models.length) {
                await loadReferenceData();
            }
            if (reloadReference || !state.templates.length) {
                await loadTemplates();
            }
            await Promise.all([
                loadTableData({ silent }),
                loadCostInsights({ manual: false }),
            ]);
        }

        function removeSelectedIds(ids = []) {
            ids.forEach((id) => state.selectedIds.delete(id));
        }

        async function runBatchAction(action, confirmMessage, successMessage) {
            const apiKeyIds = Array.from(state.selectedIds);
            if (!apiKeyIds.length) {
                showToast("请先选择至少一个 API Key", "error");
                return;
            }
            if (confirmMessage && !window.confirm(confirmMessage)) {
                return;
            }
            const buttonMap = {
                enable: batchEnableBtn,
                disable: batchDisableBtn,
                delete: batchDeleteBtn,
                rotate: batchRotateBtn,
                expire: batchExpireBtn,
            };
            const button = buttonMap[action];
            try {
                setButtonLoading(button, true);
                const result = await api.post(`/api/api-keys/batch/${action}`, { api_key_ids: apiKeyIds });
                removeSelectedIds(result.api_key_ids || apiKeyIds);
                showToast(`${successMessage} ${formatNumber(result.affected_count || 0)} 个`);
                await loadTableData({ silent: true });
            } catch (error) {
                showToast(error.message, "error");
            } finally {
                setButtonLoading(button, false);
            }
        }

        form.addEventListener("submit", async (event) => {
            event.preventDefault();
            const allowedProviderIds = getSelectedProviderIds();
            const defaultProviderId = defaultProviderSelect.value ? Number(defaultProviderSelect.value) : null;
            if (defaultProviderId && !allowedProviderIds.includes(defaultProviderId)) {
                showToast("默认中转站必须包含在授权中转站里", "error");
                return;
            }
            const payload = {
                name: nameInput.value.trim(),
                remark: remarkInput.value.trim() || null,
                enabled: enabledInput.checked,
                expires_at: expiresAtInput.value ? new Date(expiresAtInput.value).toISOString() : null,
                token_limit_total: tokenLimitInput.value === "" ? null : Number(tokenLimitInput.value),
                cost_limit_total: costLimitInput.value === "" ? null : Number(costLimitInput.value),
                request_limit_daily: requestLimitDailyInput.value === "" ? null : Number(requestLimitDailyInput.value),
                qps_limit: qpsLimitInput.value === "" ? null : Number(qpsLimitInput.value),
                rpm_limit: rpmLimitInput.value === "" ? null : Number(rpmLimitInput.value),
                tpm_limit: tpmLimitInput.value === "" ? null : Number(tpmLimitInput.value),
                route_mode: routeModeInput.value,
                default_provider_id: defaultProviderId,
                owner_user_id: ownerUserSelect.value === "" ? null : Number(ownerUserSelect.value),
                manual_allow_fallback: manualFallbackInput.checked,
                allowed_provider_ids: allowedProviderIds,
                allowed_model_names: getSelectedModelNames(),
            };
            const rawApiKey = rawApiKeyInput.value.trim();
            if (!idInput.value) {
                payload.balance_amount = balanceAmountInput.value === "" ? null : Number(balanceAmountInput.value);
                if (generationModeInput.value === "custom") {
                    const validation = updateRawApiKeyValidationState(rawApiKeyInput, rawApiKeyFeedback, { allowEmpty: false });
                    if (!validation.valid) {
                        showToast(validation.message, "error");
                        rawApiKeyInput.focus();
                        return;
                    }
                    payload.raw_api_key = rawApiKey;
                }
            } else if (rawApiKey) {
                const validation = updateRawApiKeyValidationState(rawApiKeyInput, rawApiKeyFeedback, { allowEmpty: true });
                if (!validation.valid) {
                    showToast(validation.message, "error");
                    rawApiKeyInput.focus();
                    return;
                }
                payload.raw_api_key = rawApiKey;
            }
            if (!payload.name) {
                showToast("请填写 API 密钥名称", "error");
                return;
            }
            try {
                setButtonLoading(submitBtn, true);
                if (idInput.value) {
                    await api.put(`/api/api-keys/${idInput.value}`, payload);
                    showToast("API 密钥已更新");
                    closeModal();
                } else {
                    const created = await api.post("/api/api-keys", payload);
                    idInput.value = String(created.id);
                    rawValue.textContent = created.raw_api_key;
                    rawPanel.classList.remove("hidden");
                    modalTitle.textContent = `编辑 API 密钥 #${created.id}`;
                    showToast("API 密钥已创建");
                }
                await loadData({ silent: true, reloadReference: true });
            } catch (error) {
                showToast(error.message, "error");
            } finally {
                setButtonLoading(submitBtn, false);
            }
        });

        addBtn.addEventListener("click", () => openModal());
        closeBtn.addEventListener("click", closeModal);
        cancelBtn.addEventListener("click", closeModal);
        templateAddBtn?.addEventListener("click", () => openTemplateModal());
        templateModalCloseBtn?.addEventListener("click", closeTemplateModal);
        templateModalCancelBtn?.addEventListener("click", closeTemplateModal);
        templateModal?.addEventListener("click", (event) => {
            if (event.target === templateModal) closeTemplateModal();
        });
        batchTemplateCloseBtn?.addEventListener("click", closeBatchTemplateModal);
        batchTemplateCancelBtn?.addEventListener("click", closeBatchTemplateModal);
        batchTemplateModal?.addEventListener("click", (event) => {
            if (event.target === batchTemplateModal) closeBatchTemplateModal();
        });
        batchRotateCloseBtn?.addEventListener("click", closeBatchRotateModal);
        batchRotateCancelBtn?.addEventListener("click", closeBatchRotateModal);
        batchRotateModal?.addEventListener("click", (event) => {
            if (event.target === batchRotateModal) closeBatchRotateModal();
        });
        batchRotateCopyBtn?.addEventListener("click", async () => copyText(batchRotateResult.value, batchRotateCopyBtn));
        modal.addEventListener("click", (event) => {
            if (event.target === modal) closeModal();
        });
        copyRawBtn.addEventListener("click", async () => copyText(rawValue.textContent, copyRawBtn));
        templateSelect?.addEventListener("change", () => {
            if (!templateSelect.value) return;
            applyTemplateToForm(templateSelect.value);
            showToast("已按策略模板预填当前表单");
        });
        searchInput.addEventListener("input", () => {
            window.clearTimeout(searchTimer);
            searchTimer = window.setTimeout(async () => {
                state.page = 1;
                try {
                    await loadTableData({ silent: true });
                } catch (error) {
                    showToast(error.message, "error");
                }
            }, 250);
        });
        statusFilter.addEventListener("change", async () => {
            state.page = 1;
            try {
                await loadTableData({ silent: true });
            } catch (error) {
                showToast(error.message, "error");
            }
        });
        enabledFilter.addEventListener("change", async () => {
            state.page = 1;
            try {
                await loadTableData({ silent: true });
            } catch (error) {
                showToast(error.message, "error");
            }
        });
        ownerFilter.addEventListener("change", async () => {
            state.page = 1;
            try {
                await loadTableData({ silent: true });
            } catch (error) {
                showToast(error.message, "error");
            }
        });
        pageSizeSelect.addEventListener("change", async () => {
            state.pageSize = Number.parseInt(pageSizeSelect.value || "20", 10) || 20;
            state.page = 1;
            try {
                await loadTableData({ silent: true });
            } catch (error) {
                showToast(error.message, "error");
            }
        });
        refreshBtn.addEventListener("click", async () => {
            try {
                setButtonLoading(refreshBtn, true);
                await loadData({ reloadReference: true });
            } catch (error) {
                showToast(error.message, "error");
            } finally {
                setButtonLoading(refreshBtn, false);
            }
        });
        prevPageBtn.addEventListener("click", async () => {
            if (state.page <= 1) return;
            state.page -= 1;
            try {
                await loadTableData({ silent: true });
            } catch (error) {
                showToast(error.message, "error");
            }
        });
        nextPageBtn.addEventListener("click", async () => {
            const totalPages = Math.max(1, Math.ceil((state.total || 0) / state.pageSize));
            if (state.page >= totalPages) return;
            state.page += 1;
            try {
                await loadTableData({ silent: true });
            } catch (error) {
                showToast(error.message, "error");
            }
        });
        selectPageBtn.addEventListener("click", () => {
            getVisibleApiKeyIds().forEach((id) => state.selectedIds.add(id));
            renderTable();
            setButtonTransientFeedback(selectPageBtn, "success", { successText: "已选本页" });
        });
        clearSelectionBtn.addEventListener("click", () => {
            state.selectedIds.clear();
            renderTable();
            setButtonTransientFeedback(clearSelectionBtn, "success", { successText: "已清空" });
        });
        batchEnableBtn.addEventListener("click", async () => {
            await runBatchAction("enable", null, "已批量启用");
        });
        batchDisableBtn.addEventListener("click", async () => {
            await runBatchAction("disable", null, "已批量禁用");
        });
        batchDeleteBtn.addEventListener("click", async () => {
            await runBatchAction("delete", `确认删除已选择的 ${state.selectedIds.size} 个 API Key 吗？`, "已批量删除");
        });
        batchRotateBtn.addEventListener("click", async () => {
            const apiKeyIds = Array.from(state.selectedIds);
            if (!apiKeyIds.length) {
                showToast("请先选择至少一个 API Key", "error");
                return;
            }
            if (!window.confirm(`确认轮换已选择的 ${apiKeyIds.length} 个 API Key 吗？旧密钥将立即失效。`)) {
                return;
            }
            try {
                setButtonLoading(batchRotateBtn, true);
                const result = await api.post("/api/api-keys/batch/rotate", { api_key_ids: apiKeyIds });
                openBatchRotateModal(result);
                showToast(`已批量轮换 ${formatNumber(result.affected_count || 0)} 个 API Key`);
                await loadTableData({ silent: true });
            } catch (error) {
                showToast(error.message, "error");
            } finally {
                setButtonLoading(batchRotateBtn, false);
            }
        });
        batchExpireBtn.addEventListener("click", async () => {
            await runBatchAction("expire", `确认让已选择的 ${state.selectedIds.size} 个 API Key 立即过期吗？`, "已批量过期");
        });
        batchTemplateBtn.addEventListener("click", () => {
            if (!state.selectedIds.size) {
                showToast("请先选择至少一个 API Key", "error");
                return;
            }
            openBatchTemplateModal();
        });
        batchProvidersBtn.addEventListener("click", () => {
            if (!state.selectedIds.size) {
                showToast("请先选择至少一个 API Key", "error");
                return;
            }
            openBatchProviderModal();
        });
        checkAllVisibleInput.addEventListener("change", () => {
            const visibleIds = getVisibleApiKeyIds();
            if (checkAllVisibleInput.checked) {
                visibleIds.forEach((id) => state.selectedIds.add(id));
            } else {
                visibleIds.forEach((id) => state.selectedIds.delete(id));
            }
            renderTable();
        });
        generationModeInput.addEventListener("change", refreshRawApiKeyInputState);
        rawApiKeyInput.addEventListener("input", refreshRawApiKeyInputState);
        routeModeInput.addEventListener("change", refreshRoutePreview);
        manualFallbackInput.addEventListener("change", refreshRoutePreview);
        defaultProviderSelect.addEventListener("change", refreshRoutePreview);
        providerSelector.addEventListener("change", refreshRoutePreview);
        insightGroupBySelect?.addEventListener("change", async () => {
            await loadCostInsights({ manual: false });
        });
        insightWindowDaysSelect?.addEventListener("change", async () => {
            await loadCostInsights({ manual: false });
        });
        insightRefreshBtn?.addEventListener("click", async () => {
            await loadCostInsights({ manual: true });
        });
        batchProviderCloseBtn.addEventListener("click", closeBatchProviderModal);
        batchProviderCancelBtn.addEventListener("click", closeBatchProviderModal);
        batchProviderModal.addEventListener("click", (event) => {
            if (event.target === batchProviderModal) closeBatchProviderModal();
        });
        batchProviderForm.addEventListener("submit", async (event) => {
            event.preventDefault();
            const allowedProviderIds = getBatchSelectedProviderIds();
            const defaultProviderId = batchProviderDefault.value ? Number(batchProviderDefault.value) : null;
            if (defaultProviderId && !allowedProviderIds.includes(defaultProviderId)) {
                showToast("默认中转站必须包含在授权中转站里", "error");
                return;
            }
            try {
                setButtonLoading(batchProviderSubmitBtn, true);
                const result = await api.post("/api/api-keys/batch/providers", {
                    api_key_ids: Array.from(state.selectedIds),
                    route_mode: batchProviderRouteMode.value,
                    default_provider_id: defaultProviderId,
                    manual_allow_fallback: batchProviderFallback.checked,
                    allowed_provider_ids: allowedProviderIds,
                });
                showToast(`已批量更新渠道授权 ${formatNumber(result.affected_count || 0)} 个`);
                closeBatchProviderModal();
                await loadTableData({ silent: true });
            } catch (error) {
                showToast(error.message, "error");
            } finally {
                setButtonLoading(batchProviderSubmitBtn, false);
            }
        });

        templateForm?.addEventListener("submit", async (event) => {
            event.preventDefault();
            const allowedProviderIds = getTemplateSelectedProviderIds();
            const defaultProviderId = templateDefaultProviderSelect.value ? Number(templateDefaultProviderSelect.value) : null;
            if (defaultProviderId && !allowedProviderIds.includes(defaultProviderId)) {
                showToast("默认中转站必须包含在授权中转站里", "error");
                return;
            }
            const payload = {
                name: templateNameInput.value.trim(),
                remark: templateRemarkInput.value.trim() || null,
                enabled: templateEnabledInput.checked,
                route_mode: templateRouteModeInput.value,
                default_provider_id: defaultProviderId,
                manual_allow_fallback: templateManualFallbackInput.checked,
                token_limit_total: templateTokenLimitInput.value === "" ? null : Number(templateTokenLimitInput.value),
                cost_limit_total: templateCostLimitInput.value === "" ? null : Number(templateCostLimitInput.value),
                expires_in_days: templateExpiresInDaysInput.value === "" ? null : Number(templateExpiresInDaysInput.value),
                allowed_provider_ids: allowedProviderIds,
                allowed_model_names: getTemplateSelectedModelNames(),
            };
            try {
                setButtonLoading(templateSubmitBtn, true);
                if (templateEditIdInput.value) {
                    await api.put(`/api/api-key-policy-templates/${templateEditIdInput.value}`, payload);
                    showToast("策略模板已更新");
                } else {
                    await api.post("/api/api-key-policy-templates", payload);
                    showToast("策略模板已创建");
                }
                closeTemplateModal();
                await loadData({ silent: true, reloadReference: true });
            } catch (error) {
                showToast(error.message, "error");
            } finally {
                setButtonLoading(templateSubmitBtn, false);
            }
        });

        batchTemplateForm?.addEventListener("submit", async (event) => {
            event.preventDefault();
            const templateId = Number(batchTemplateSelect.value);
            if (!Number.isFinite(templateId)) {
                showToast("请选择一个策略模板", "error");
                return;
            }
            try {
                setButtonLoading(batchTemplateSubmitBtn, true);
                const result = await api.post("/api/api-keys/batch/template", {
                    api_key_ids: Array.from(state.selectedIds),
                    template_id: templateId,
                });
                showToast(`已批量套用模板 ${formatNumber(result.affected_count || 0)} 个`);
                closeBatchTemplateModal();
                await loadTableData({ silent: true });
            } catch (error) {
                showToast(error.message, "error");
            } finally {
                setButtonLoading(batchTemplateSubmitBtn, false);
            }
        });

        templateTableBody?.addEventListener("click", async (event) => {
            const button = event.target.closest("[data-action][data-template-id]");
            if (!button) return;
            const templateId = Number(button.dataset.templateId);
            const template = state.templates.find((item) => item.id === templateId);
            if (!template) return;
            if (button.dataset.action === "edit-template") {
                openTemplateModal(template);
                return;
            }
            if (button.dataset.action === "delete-template") {
                if (!window.confirm(`确认删除策略模板「${template.name}」吗？`)) return;
                try {
                    await api.delete(`/api/api-key-policy-templates/${templateId}`);
                    showToast("策略模板已删除");
                    await loadData({ silent: true, reloadReference: true });
                } catch (error) {
                    showToast(error.message, "error");
                }
            }
        });

        tableBody.addEventListener("click", async (event) => {
            const button = event.target.closest("[data-action]");
            if (!button) return;
            const apiKeyId = Number(button.dataset.apiKeyId);
            const apiKey = state.apiKeys.find((item) => item.id === apiKeyId);
            if (!apiKey) return;

            if (button.dataset.action === "detail") {
                await navigateWithinShell(`/api-keys/${apiKeyId}`);
                return;
            }
            if (button.dataset.action === "edit") {
                openModal(apiKey);
                return;
            }
            if (button.dataset.action === "delete") {
                if (!window.confirm(`确认删除 API 密钥「${apiKey.name}」吗？`)) return;
                try {
                    await api.delete(`/api/api-keys/${apiKeyId}`);
                    state.selectedIds.delete(apiKeyId);
                    showToast("API 密钥已删除");
                    await loadTableData({ silent: true });
                } catch (error) {
                    showToast(error.message, "error");
                }
                return;
            }
            if (button.dataset.action === "enable" || button.dataset.action === "disable") {
                try {
                    await api.post(`/api/api-keys/${apiKeyId}/${button.dataset.action}`);
                    showToast(`API 密钥已${button.dataset.action === "enable" ? "启用" : "禁用"}`);
                    await loadTableData({ silent: true });
                } catch (error) {
                    showToast(error.message, "error");
                }
            }
        });
        tableBody.addEventListener("change", (event) => {
            const checkbox = event.target.closest("[data-api-key-select]");
            if (!checkbox) return;
            const apiKeyId = Number(checkbox.dataset.apiKeySelect);
            if (!Number.isFinite(apiKeyId)) return;
            if (checkbox.checked) {
                state.selectedIds.add(apiKeyId);
            } else {
                state.selectedIds.delete(apiKeyId);
            }
            renderBatchButtons();
            renderPagination();
        });

        await loadData({ silent: true, reloadReference: true });
    }

    async function initApiKeyDetail() {
        const hero = document.querySelector("[data-api-key-id]");
        if (!hero) return;
        const apiKeyId = Number(hero.dataset.apiKeyId);
        if (!Number.isFinite(apiKeyId)) return;

        const refreshBtn = document.getElementById("api-key-detail-refresh-btn");
        const logsTableBody = document.getElementById("api-key-detail-logs");
        const logModal = document.getElementById("api-key-log-modal");
        const logModalContent = document.getElementById("api-key-log-modal-content");
        const logModalClose = document.getElementById("api-key-log-modal-close");
        const openFullLogsLink = document.getElementById("api-key-detail-open-full-logs");
        const billingTableBody = document.getElementById("api-key-detail-billing-records");
        const balanceAdjustForm = document.getElementById("api-key-balance-adjust-form");
        const balanceAdjustAmountInput = document.getElementById("api-key-balance-adjust-amount");
        const balanceAdjustRemarkInput = document.getElementById("api-key-balance-adjust-remark");
        const balanceAdjustSubmit = document.getElementById("api-key-balance-adjust-submit");

        function renderBilling(billing) {
            document.getElementById("api-key-detail-billing-summary").innerHTML = `
                <div><span>当前余额</span><strong>${billing.balance_amount == null ? "不限" : formatMoney(billing.balance_amount)}</strong></div>
                <div><span>累计消费</span><strong>${formatMoney(billing.total_cost_used)}</strong></div>
                <div><span>累计充值</span><strong>${formatMoney(billing.total_recharge_amount)}</strong></div>
                <div><span>金额额度</span><strong>${billing.cost_limit_total == null ? "不限" : formatMoney(billing.cost_limit_total)}</strong></div>
                <div><span>剩余额度</span><strong>${billing.remaining_cost_quota == null ? "不限" : formatMoney(billing.remaining_cost_quota)}</strong></div>
                <div><span>24h 消费</span><strong>${formatMoney(billing.recent_billed_cost)}</strong></div>
                <div><span>账单笔数</span><strong>${formatNumber(billing.total_billing_records)}</strong></div>
            `;
            billingTableBody.innerHTML = billing.items.length
                ? billing.items.map((item) => `
                    <tr>
                        <td>${formatDate(item.created_at)}</td>
                        <td>${escapeHtml(item.record_type === "request_charge" ? "请求扣费" : (item.record_type === "top_up" ? "充值" : "手工调整"))}</td>
                        <td>${escapeHtml(formatMoney(item.amount))}</td>
                        <td>${escapeHtml(item.balance_after == null ? "不限" : formatMoney(item.balance_after))}</td>
                        <td>
                            <strong>${escapeHtml(item.model_name || "-")}</strong>
                            <div class="table-muted">${escapeHtml(item.provider_name || "-")}</div>
                        </td>
                        <td>${formatNumber(item.total_tokens ?? 0)}</td>
                        <td>${escapeHtml(item.remark || "-")}</td>
                    </tr>
                `).join("")
                : '<tr><td colspan="7"><div class="empty-state">暂无账单流水</div></td></tr>';
        }

        function renderDetail(detail, stats, analytics, logs, billing) {
            document.getElementById("api-key-detail-title").textContent = detail.name;
            document.getElementById("api-key-detail-subtitle").textContent = `${renderApiKeyStatusText(detail.status)} · ${detail.key_masked} · 最近使用 ${formatDate(detail.last_used_at)}`;
            const quota = buildApiKeyQuotaSummary(detail);
            document.getElementById("api-key-detail-signal").innerHTML = `
                <div class="cockpit-aside-label">额度脉冲</div>
                <div class="cockpit-aside-value">${detail.balance_amount == null ? (detail.remaining_tokens == null ? "∞" : formatNumber(detail.remaining_tokens)) : formatMoney(detail.balance_amount)}</div>
                <div class="cockpit-aside-copy">余额、额度与最近调用强度</div>
                <div class="cockpit-health-bar"><span style="width:${quota.percent}%"></span></div>
                <div class="cockpit-aside-meta">
                    <span>状态 ${escapeHtml(renderApiKeyStatusText(detail.status))}</span>
                    <span>消费 ${formatMoney(detail.total_cost_used)}</span>
                </div>
            `;
            document.getElementById("api-key-detail-total-requests").textContent = formatNumber(stats.total_requests);
            document.getElementById("api-key-detail-prompt-tokens").textContent = formatNumber(detail.prompt_tokens_used);
            document.getElementById("api-key-detail-completion-tokens").textContent = formatNumber(detail.completion_tokens_used);
            document.getElementById("api-key-detail-total-tokens").textContent = formatNumber(detail.total_tokens_used);
            document.getElementById("api-key-detail-total-cost-used").textContent = formatMoney(detail.total_cost_used);
            document.getElementById("api-key-detail-balance-amount").textContent = detail.balance_amount == null ? "不限" : formatMoney(detail.balance_amount);
            document.getElementById("api-key-detail-recent-requests").textContent = formatNumber(stats.recent_requests);
            document.getElementById("api-key-detail-recent-failures").textContent = formatNumber(stats.recent_failed_requests);
            document.getElementById("api-key-detail-meta").innerHTML = `
                <div><span>名称</span><strong>${escapeHtml(detail.name)}</strong></div>
                <div><span>备注</span><strong>${escapeHtml(detail.remark || "-")}</strong></div>
                <div><span>状态</span><strong>${escapeHtml(renderApiKeyStatusText(detail.status))}</strong></div>
                <div><span>前缀</span><strong>${escapeHtml(detail.key_masked)}</strong></div>
                <div><span>密钥明文</span><strong>${detail.raw_api_key ? `${escapeHtml(detail.raw_api_key)} <button class="btn btn-ghost btn-sm interactive-btn" type="button" data-copy-text="${escapeHtml(detail.raw_api_key)}">复制</button>` : "历史密钥未存明文，可在编辑时替换为新密钥"}</strong></div>
                <div><span>默认中转</span><strong>${escapeHtml(detail.default_provider_id ? String(detail.default_provider_id) : "-")}</strong></div>
                <div><span>过期时间</span><strong>${formatDate(detail.expires_at)}</strong></div>
                <div><span>每日请求上限</span><strong>${formatLimitSetting(detail.request_limit_daily)}</strong></div>
                <div><span>QPS / RPM / TPM</span><strong>${formatLimitSetting(detail.qps_limit)} / ${formatLimitSetting(detail.rpm_limit)} / ${formatLimitSetting(detail.tpm_limit)}</strong></div>
                <div><span>最近使用</span><strong>${formatDate(detail.last_used_at)}</strong></div>
                <div><span>更新时间</span><strong>${formatDate(detail.updated_at)}</strong></div>
            `;
            document.getElementById("api-key-detail-quota-bar").style.width = `${quota.percent}%`;
            document.getElementById("api-key-detail-quota-meta").innerHTML = `
                <div><span>总额度</span><strong>${detail.token_limit_total == null ? "无限额" : formatNumber(detail.token_limit_total)}</strong></div>
                <div><span>已使用</span><strong>${formatNumber(detail.total_tokens_used)}</strong></div>
                <div><span>剩余额度</span><strong>${detail.remaining_tokens == null ? "无限额" : formatNumber(detail.remaining_tokens)}</strong></div>
                <div><span>24h Token</span><strong>${formatNumber(detail.recent_usage.recent_total_tokens)} Token</strong></div>
                <div><span>金额额度</span><strong>${detail.cost_limit_total == null ? "不限" : formatMoney(detail.cost_limit_total)}</strong></div>
                <div><span>当前余额</span><strong>${detail.balance_amount == null ? "不限" : formatMoney(detail.balance_amount)}</strong></div>
                <div><span>每日请求</span><strong>${formatLimitSetting(detail.request_limit_daily)}</strong></div>
                <div><span>QPS / RPM / TPM</span><strong>${formatLimitSetting(detail.qps_limit)} / ${formatLimitSetting(detail.rpm_limit)} / ${formatLimitSetting(detail.tpm_limit)}</strong></div>
                <div><span>24h 消费</span><strong>${formatMoney(stats.recent_total_cost)}</strong></div>
            `;
            document.getElementById("api-key-detail-bindings").innerHTML = detail.allowed_providers.length
                ? detail.allowed_providers.map((provider) => `
                    <article class="api-key-chip-card">
                        <strong>${escapeHtml(provider.name)}</strong>
                        <span>${escapeHtml(provider.enabled ? "已启用" : "已禁用")}</span>
                        <div>${statusBadge(provider.health_status)}</div>
                    </article>
                `).join("")
                : '<div class="empty-state">当前没有授权中转站</div>';
            renderRoutePolicyGuideInto({
                modeCards: document.getElementById("api-key-detail-route-cards"),
                liveSummary: document.getElementById("api-key-detail-route-summary"),
                routeMode: detail.route_mode,
                defaultProviderId: detail.default_provider_id,
                manualAllowFallback: detail.manual_allow_fallback,
                providers: detail.allowed_providers.map((provider) => ({ ...provider, id: provider.id })),
            });
            document.getElementById("api-key-detail-model-distribution").innerHTML = analytics.model_distribution.length
                ? analytics.model_distribution.map((item) => `
                    <article class="api-key-telemetry-card">
                        <div class="api-key-telemetry-head">
                            <strong>${escapeHtml(item.model_name)}</strong>
                            <span>${formatDate(item.last_requested_at)}</span>
                        </div>
                        <div class="api-key-telemetry-metrics">
                            <span>请求 ${formatNumber(item.total_requests)}</span>
                            <span>失败 ${formatNumber(item.failed_requests)}</span>
                            <span>Token ${formatNumber(item.total_tokens)}</span>
                            <span>消费 ${formatMoney(item.total_cost)}</span>
                        </div>
                    </article>
                `).join("")
                : '<div class="empty-state">暂无模型调用记录</div>';
            document.getElementById("api-key-detail-errors").innerHTML = analytics.recent_errors.length
                ? analytics.recent_errors.map((item) => `
                    <article class="api-key-error-card">
                        <div class="api-key-error-head">
                            <strong>${escapeHtml(item.message || "未记录错误信息")}</strong>
                            <span>${formatDate(item.created_at)}</span>
                        </div>
                        <div class="api-key-error-meta">
                            <span>${escapeHtml(item.request_path || "-")}</span>
                            <span>${escapeHtml(item.model_name || "-")}</span>
                            <span>${escapeHtml(item.provider_name || "-")}</span>
                            <span>${item.status_code ?? "-"}</span>
                            <span>${escapeHtml(item.api_client_auth_result ? formatApiClientAuthResultLabel(item.api_client_auth_result) : "-")}</span>
                        </div>
                    </article>
                `).join("")
                : '<div class="empty-state">暂无错误记录</div>';

            logsTableBody.innerHTML = logs.items.map((log) => `
                <tr>
                    <td>${formatDate(log.created_at)}</td>
                    <td>${escapeHtml(formatLogTypeLabel(log.log_type))}</td>
                    <td>${escapeHtml(log.requested_model || log.model_name || "-")}</td>
                    <td>${escapeHtml(log.provider_name || "-")}</td>
                    <td>${escapeHtml(log.api_client_auth_result ? formatApiClientAuthResultLabel(log.api_client_auth_result) : (log.success ? formatApiClientAuthResultLabel("authenticated") : "-"))}</td>
                    <td>${formatNumber(log.total_tokens ?? 0)}<div class="table-muted">${escapeHtml(log.total_cost == null ? "-" : formatMoney(log.total_cost))}</div></td>
                    <td>${log.status_code ?? "-"}</td>
                    <td>${log.latency_ms ?? "-"}</td>
                    <td>${log.success ? statusBadge("healthy") : statusBadge("unhealthy")}</td>
                    <td><button class="table-action-btn" data-action="show-api-key-log" data-log='${escapeHtml(JSON.stringify(log))}'>详情</button></td>
                </tr>
            `).join("") || '<tr><td colspan="10"><div class="empty-state">暂无请求日志</div></td></tr>';
            enhanceInteractiveButtons(logsTableBody);
            openFullLogsLink.href = `/logs?api_client_key_id=${encodeURIComponent(String(apiKeyId))}`;
            renderBilling(billing);
        }

        async function loadDetail({ silent = false } = {}) {
            const [detail, stats, analytics, logs, billing] = await Promise.all([
                api.get(`/api/api-keys/${apiKeyId}`),
                api.get(`/api/api-keys/${apiKeyId}/stats`),
                api.get(`/api/api-keys/${apiKeyId}/analytics`),
                api.get(`/api/api-keys/${apiKeyId}/logs?page=1&page_size=20`),
                api.get(`/api/api-keys/${apiKeyId}/billing?limit=20`),
            ]);
            renderDetail(detail, stats, analytics, logs, billing);
            if (!silent) showToast("密钥详情已刷新");
        }

        refreshBtn.addEventListener("click", async () => {
            try {
                setButtonLoading(refreshBtn, true);
                await loadDetail();
            } catch (error) {
                showToast(error.message, "error");
            } finally {
                setButtonLoading(refreshBtn, false);
            }
        });

        logModalClose.addEventListener("click", () => logModal.classList.add("hidden"));
        logModal.addEventListener("click", (event) => {
            if (event.target === logModal) logModal.classList.add("hidden");
        });
        logsTableBody.addEventListener("click", (event) => {
            const button = event.target.closest('[data-action="show-api-key-log"]');
            if (!button) return;
            const log = safeJsonParse(button.dataset.log || "") || {};
            logModalContent.textContent = JSON.stringify(
                {
                    ...log,
                    request_body_json: safeJsonParse(log.request_body_json || "") ?? log.request_body_json,
                    response_body_json: safeJsonParse(log.response_body_json || "") ?? log.response_body_json,
                    trace_json: safeJsonParse(log.trace_json || "") ?? log.trace_json,
                    api_client_policy_snapshot_json: safeJsonParse(log.api_client_policy_snapshot_json || "") ?? log.api_client_policy_snapshot_json,
                },
                null,
                2,
            );
            logModal.classList.remove("hidden");
        });

        balanceAdjustForm.addEventListener("submit", async (event) => {
            event.preventDefault();
            if (!balanceAdjustAmountInput.value.trim()) {
                showToast("请填写调整金额", "error");
                return;
            }
            try {
                setButtonLoading(balanceAdjustSubmit, true);
                await api.post(`/api/api-keys/${apiKeyId}/billing/adjust`, {
                    amount: Number(balanceAdjustAmountInput.value),
                    remark: balanceAdjustRemarkInput.value.trim() || null,
                });
                balanceAdjustForm.reset();
                showToast("余额调整已提交");
                await loadDetail({ silent: true });
            } catch (error) {
                showToast(error.message, "error");
            } finally {
                setButtonLoading(balanceAdjustSubmit, false);
            }
        });

        await loadDetail({ silent: true });
    }

    async function initLogs() {
        const tableBody = document.getElementById("logs-table-body");
        const refreshBtn = document.getElementById("logs-refresh-btn");
        const exportBtn = document.getElementById("logs-export-btn");
        const lastRefreshLabel = document.getElementById("logs-last-refresh");
        const clearBtn = document.getElementById("logs-clear-btn");
        const providerSelect = document.getElementById("logs-provider-id");
        const modelSelect = document.getElementById("logs-model-name");
        const modelQueryInput = document.getElementById("logs-model-query");
        const userAccountSelect = document.getElementById("logs-user-account-id");
        const userAccountQueryInput = document.getElementById("logs-user-account-query");
        const apiClientKeyIdSelect = document.getElementById("logs-api-client-key-id");
        const apiClientKeyQuerySelect = document.getElementById("logs-api-client-key-query");
        const apiClientKeyQueryManualInput = document.getElementById("logs-api-client-key-query-manual");
        const tenantNameInput = document.getElementById("logs-tenant-name");
        const projectNameInput = document.getElementById("logs-project-name");
        const appNameInput = document.getElementById("logs-app-name");
        const environmentNameInput = document.getElementById("logs-environment-name");
        const excludeHealthChecksInput = document.getElementById("logs-exclude-health-checks");
        const pageSizeSelect = document.getElementById("logs-page-size");
        const pageMeta = document.getElementById("logs-page-meta");
        const prevPageBtn = document.getElementById("logs-prev-page-btn");
        const nextPageBtn = document.getElementById("logs-next-page-btn");
        const traceModal = document.getElementById("log-trace-modal");
        const traceContent = document.getElementById("log-trace-content");
        const immediateFilterIds = [
            "logs-log-type",
            "logs-provider-id",
            "logs-model-name",
            "logs-user-account-id",
            "logs-api-client-key-id",
            "logs-api-client-key-query",
            "logs-success",
            "logs-tenant-name",
            "logs-project-name",
            "logs-app-name",
            "logs-environment-name",
        ];
        const debouncedFilterIds = [
            "logs-model-query",
            "logs-user-account-query",
            "logs-api-client-key-query-manual",
            "logs-conversation-key",
        ];
        const currentParams = new URLSearchParams(window.location.search);
        const state = {
            page: Math.max(1, Number.parseInt(currentParams.get("page") || "1", 10) || 1),
            pageSize: [50, 100, 200].includes(Number.parseInt(currentParams.get("page_size") || "50", 10))
                ? Number.parseInt(currentParams.get("page_size") || "50", 10)
                : 50,
            total: 0,
        };
        let initialFilterValuesApplied = false;
        if (currentParams.get("conversation_key")) {
            document.getElementById("logs-conversation-key").value = currentParams.get("conversation_key");
        }
        if (currentParams.get("model_query")) {
            modelQueryInput.value = currentParams.get("model_query");
        }
        if (currentParams.get("user_account_query")) {
            userAccountQueryInput.value = currentParams.get("user_account_query");
        }
        if (currentParams.get("api_client_key_query_text")) {
            apiClientKeyQueryManualInput.value = currentParams.get("api_client_key_query_text");
        }
        if (currentParams.get("exclude_health_checks") === "false") {
            excludeHealthChecksInput.checked = false;
        }
        pageSizeSelect.value = String(state.pageSize);

        for (const id of immediateFilterIds) {
            document.getElementById(id).addEventListener("change", () => {
                state.page = 1;
                loadLogs();
            });
        }
        const debouncedLoadLogs = debounce(() => {
            state.page = 1;
            loadLogs();
        });
        for (const id of debouncedFilterIds) {
            document.getElementById(id).addEventListener("input", () => {
                state.page = 1;
                debouncedLoadLogs();
            });
        }
        pageSizeSelect.addEventListener("change", () => {
            state.pageSize = Number.parseInt(pageSizeSelect.value || "50", 10) || 50;
            state.page = 1;
            loadLogs();
        });
        prevPageBtn.addEventListener("click", async () => {
            if (state.page <= 1) return;
            state.page -= 1;
            await loadLogs();
        });
        nextPageBtn.addEventListener("click", async () => {
            const totalPages = Math.max(1, Math.ceil((state.total || 0) / state.pageSize));
            if (state.page >= totalPages) return;
            state.page += 1;
            await loadLogs();
        });
        exportBtn.addEventListener("click", () => {
            const params = new URLSearchParams();
            const logType = document.getElementById("logs-log-type").value;
            const providerId = providerSelect.value;
            const modelName = modelSelect.value;
            const modelQuery = modelQueryInput.value.trim();
            const userAccountId = userAccountSelect.value;
            const userAccountQuery = userAccountQueryInput.value.trim();
            const apiClientKeyId = apiClientKeyIdSelect.value;
            const apiClientKeyQuery = apiClientKeyQueryManualInput.value.trim() || apiClientKeyQuerySelect.value;
            const success = document.getElementById("logs-success").value;
            const conversationKey = document.getElementById("logs-conversation-key").value.trim();
            const tenantName = tenantNameInput.value.trim();
            const projectName = projectNameInput.value.trim();
            const appName = appNameInput.value.trim();
            const environmentName = environmentNameInput.value.trim();
            if (logType) params.set("log_type", logType);
            if (providerId) params.set("provider_id", providerId);
            if (modelName) params.set("model_name", modelName);
            if (modelQuery) params.set("model_query", modelQuery);
            if (userAccountId) params.set("user_account_id", userAccountId);
            if (userAccountQuery) params.set("user_account_query", userAccountQuery);
            if (apiClientKeyId) params.set("api_client_key_id", apiClientKeyId);
            if (apiClientKeyQuery) params.set("api_client_key_query", apiClientKeyQuery);
            if (apiClientKeyQueryManualInput.value.trim()) params.set("api_client_key_query_text", apiClientKeyQueryManualInput.value.trim());
            if (success) params.set("success", success);
            if (conversationKey) params.set("conversation_key", conversationKey);
            if (tenantName) params.set("tenant_name", tenantName);
            if (projectName) params.set("project_name", projectName);
            if (appName) params.set("app_name", appName);
            if (environmentName) params.set("environment_name", environmentName);
            params.set("exclude_health_checks", excludeHealthChecksInput.checked ? "true" : "false");
            params.set("limit", "5000");
            setButtonTransientFeedback(exportBtn, "success", { successText: "准备导出" });
            window.location.href = `/api/logs/export?${params.toString()}`;
        });

        refreshBtn.addEventListener("click", async (event) => {
            event.preventDefault();
            await loadFilterOptions();
            await loadLogs({ manual: true });
        });
        document.getElementById("log-trace-close").addEventListener("click", () => traceModal.classList.add("hidden"));
        traceModal.addEventListener("click", (event) => {
            if (event.target === traceModal) traceModal.classList.add("hidden");
        });
        clearBtn.addEventListener("click", async () => {
            if (!window.confirm("确认清空全部日志吗？")) return;
            try {
                setButtonLoading(clearBtn, true);
                await api.delete("/api/logs");
                showToast("日志已清空");
                await loadFilterOptions();
                await loadLogs({ manual: true });
            } catch (error) {
                showToast(error.message, "error");
            } finally {
                setButtonLoading(clearBtn, false);
            }
        });

        function renderLogSummary(summary) {
            document.querySelectorAll("[data-log-summary]").forEach((node) => {
                const key = node.dataset.logSummary;
                node.textContent = formatNumber(summary?.[key] ?? 0);
            });
        }

        function formatMetricValue(value, suffix = "") {
            if (value == null || Number.isNaN(Number(value))) return "-";
            return `${formatNumber(Number(value))}${suffix}`;
        }

        function formatRateValue(value) {
            if (value == null || Number.isNaN(Number(value))) return "-";
            return Number(value).toFixed(2);
        }

        function buildSessionValue(log) {
            return log.session_id || log.conversation_key || log.request_id || "-";
        }

        function renderSelectOptions(selectNode, items, emptyLabel = "全部") {
            const previousValue = selectNode.value;
            selectNode.innerHTML = [`<option value="">${escapeHtml(emptyLabel)}</option>`]
                .concat(
                    (items || []).map((item) => (
                        `<option value="${escapeHtml(item.value)}">${escapeHtml(item.label)}</option>`
                    ))
                )
                .join("");
            if (previousValue && Array.from(selectNode.options).some((option) => option.value === previousValue)) {
                selectNode.value = previousValue;
            }
        }

        async function loadFilterOptions() {
            const params = new URLSearchParams({
                exclude_health_checks: excludeHealthChecksInput.checked ? "true" : "false",
                _ts: Date.now().toString(),
            });
            const data = await api.get(`/api/logs/filter-options?${params.toString()}`);
            renderSelectOptions(providerSelect, data.providers);
            renderSelectOptions(modelSelect, data.model_names);
            renderSelectOptions(userAccountSelect, data.users);
            renderSelectOptions(apiClientKeyIdSelect, data.api_client_key_ids);
            renderSelectOptions(apiClientKeyQuerySelect, data.api_client_key_queries);
            renderSelectOptions(tenantNameInput, data.tenants);
            renderSelectOptions(projectNameInput, data.projects);
            renderSelectOptions(appNameInput, data.apps);
            renderSelectOptions(environmentNameInput, data.environments);
            if (!initialFilterValuesApplied && currentParams.get("provider_id")) {
                providerSelect.value = currentParams.get("provider_id");
            }
            if (!initialFilterValuesApplied && currentParams.get("model_name")) {
                modelSelect.value = currentParams.get("model_name");
            }
            if (!initialFilterValuesApplied && currentParams.get("user_account_id")) {
                userAccountSelect.value = currentParams.get("user_account_id");
            }
            if (!initialFilterValuesApplied && currentParams.get("api_client_key_id")) {
                apiClientKeyIdSelect.value = currentParams.get("api_client_key_id");
            }
            if (!initialFilterValuesApplied && currentParams.get("api_client_key_query")) {
                apiClientKeyQuerySelect.value = currentParams.get("api_client_key_query");
            }
            if (!initialFilterValuesApplied && currentParams.get("tenant_name")) {
                tenantNameInput.value = currentParams.get("tenant_name");
            }
            if (!initialFilterValuesApplied && currentParams.get("project_name")) {
                projectNameInput.value = currentParams.get("project_name");
            }
            if (!initialFilterValuesApplied && currentParams.get("app_name")) {
                appNameInput.value = currentParams.get("app_name");
            }
            if (!initialFilterValuesApplied && currentParams.get("environment_name")) {
                environmentNameInput.value = currentParams.get("environment_name");
            }
            if (!initialFilterValuesApplied && currentParams.get("log_type")) {
                document.getElementById("logs-log-type").value = currentParams.get("log_type");
            }
            if (!initialFilterValuesApplied && currentParams.get("success")) {
                document.getElementById("logs-success").value = currentParams.get("success");
            }
            initialFilterValuesApplied = true;
        }

        function renderLogPagination(total) {
            state.total = Number(total || 0);
            const totalPages = Math.max(1, Math.ceil(state.total / state.pageSize));
            if (state.page > totalPages) {
                state.page = totalPages;
            }
            pageMeta.textContent = `第 ${formatNumber(state.page)} 页，共 ${formatNumber(totalPages)} 页 · 共 ${formatNumber(state.total)} 条`;
            prevPageBtn.disabled = state.page <= 1;
            nextPageBtn.disabled = state.page >= totalPages;
        }
        
        async function loadLogs({ manual = false } = {}) {
            setButtonLoading(refreshBtn, true);
            const params = new URLSearchParams({
                page: String(state.page),
                page_size: String(state.pageSize),
            });
            const logType = document.getElementById("logs-log-type").value;
            const providerId = providerSelect.value;
            const modelName = modelSelect.value;
            const modelQuery = modelQueryInput.value.trim();
            const userAccountId = userAccountSelect.value;
            const userAccountQuery = userAccountQueryInput.value.trim();
            const apiClientKeyId = apiClientKeyIdSelect.value;
            const apiClientKeyQuery = apiClientKeyQueryManualInput.value.trim() || apiClientKeyQuerySelect.value;
            const success = document.getElementById("logs-success").value;
            const conversationKey = document.getElementById("logs-conversation-key").value.trim();
            const tenantName = tenantNameInput.value.trim();
            const projectName = projectNameInput.value.trim();
            const appName = appNameInput.value.trim();
            const environmentName = environmentNameInput.value.trim();
            const excludeHealthChecks = excludeHealthChecksInput.checked;
            if (logType) params.set("log_type", logType);
            if (providerId) params.set("provider_id", providerId);
            if (modelName) params.set("model_name", modelName);
            if (modelQuery) params.set("model_query", modelQuery);
            if (userAccountId) params.set("user_account_id", userAccountId);
            if (userAccountQuery) params.set("user_account_query", userAccountQuery);
            if (apiClientKeyId) params.set("api_client_key_id", apiClientKeyId);
            if (apiClientKeyQuery) params.set("api_client_key_query", apiClientKeyQuery);
            if (success) params.set("success", success);
            if (conversationKey) params.set("conversation_key", conversationKey);
            if (tenantName) params.set("tenant_name", tenantName);
            if (projectName) params.set("project_name", projectName);
            if (appName) params.set("app_name", appName);
            if (environmentName) params.set("environment_name", environmentName);
            params.set("exclude_health_checks", excludeHealthChecks ? "true" : "false");
            params.set("_ts", Date.now().toString());
            try {
                const data = await api.get(`/api/logs?${params.toString()}`);
                renderLogPagination(data.total ?? data.items.length);
                renderLogSummary(data.summary || {});
                tableBody.innerHTML = data.items.map((log) => `
                    <tr>
                        <td>
                            <strong>${formatDate(log.created_at)}</strong>
                            <div class="table-muted">${escapeHtml(log.http_method || "-")}</div>
                        </td>
                        <td>
                            <strong>${escapeHtml(formatLogTypeLabel(log.log_type))}</strong>
                            <div class="table-muted">思维等级 ${escapeHtml(log.reasoning_level || "无")}${log.model_reasoning_effort ? ` · 参数 ${escapeHtml(log.model_reasoning_effort)}` : ""}</div>
                        </td>
                        <td>
                            <strong>${escapeHtml(log.user_account_name || "-")}</strong>
                            <div class="table-muted">${escapeHtml(log.api_client_key_name || "-")}</div>
                            <div class="table-muted">${escapeHtml(log.api_client_key_prefix || "-")}</div>
                        </td>
                        <td>${escapeHtml(buildSessionValue(log))}</td>
                        <td>${escapeHtml(log.requested_model || log.model_name || "-")}</td>
                        <td>${escapeHtml(log.provider_name || "-")}</td>
                        <td>${renderLogResultCell(log)}</td>
                        <td>${renderLogBillingCell(log)}</td>
                        <td>
                            <strong>${formatMetricValue(log.duration_ms ?? log.latency_ms, " ms")}</strong>
                            <div class="table-muted">TTFB ${formatMetricValue(log.ttfb_ms, " ms")} · TPS ${formatRateValue(log.tps)}</div>
                        </td>
                        <td>
                            <div class="table-actions">
                                <button class="table-action-btn" data-action="show-trace" data-log-id="${log.id}">详情</button>
                                ${log.conversation_key ? `<button class="table-action-btn" data-action="open-conversation" data-conversation-key="${encodeURIComponent(log.conversation_key)}">回放</button>` : ""}
                            </div>
                        </td>
                    </tr>
                `).join("") || '<tr><td colspan="10"><div class="empty-state">暂无日志</div></td></tr>';
                tableBody.querySelectorAll('button[data-action="show-trace"]').forEach((button, index) => {
                    button.dataset.log = JSON.stringify(data.items[index] || {});
                });
                enhanceInteractiveButtons(tableBody);
                if (lastRefreshLabel) {
                    lastRefreshLabel.textContent = `最近刷新: ${formatDate(new Date().toISOString())}`;
                }
                if (manual) {
                    showToast(`日志已刷新，第 ${state.page} 页 / ${Math.max(1, Math.ceil((state.total || 0) / state.pageSize))} 页`);
                }
            } catch (error) {
                showToast(error.message, "error");
            } finally {
                setButtonLoading(refreshBtn, false);
            }
        }

        excludeHealthChecksInput.addEventListener("change", async () => {
            state.page = 1;
            await loadFilterOptions();
            await loadLogs();
        });

        tableBody.addEventListener("click", (event) => {
            const button = event.target.closest('button[data-action]');
            if (!button) return;
            if (button.dataset.action === "open-conversation") {
                const conversationKey = decodeURIComponent(button.dataset.conversationKey);
                const target = `/conversations?conversation_key=${encodeURIComponent(conversationKey)}`;
                navigateWithinShell(target).catch(() => {
                    window.location.href = target;
                });
                return;
            }
            if (button.dataset.action !== "show-trace") return;
            const log = safeJsonParse(button.dataset.log || "") || {};
            const detail = {
                id: log.id,
                log_type: log.log_type,
                request_id: log.request_id,
                conversation_key: log.conversation_key,
                session_id: log.session_id,
                requested_model: log.requested_model,
                model_name: log.model_name,
                provider_name: log.provider_name,
                user_account_id: log.user_account_id,
                user_account_name: log.user_account_name,
                api_client_key_id: log.api_client_key_id,
                api_client_key_name: log.api_client_key_name,
                api_client_key_prefix: log.api_client_key_prefix,
                api_client_auth_result: log.api_client_auth_result,
                api_client_remaining_tokens: log.api_client_remaining_tokens,
                http_method: log.http_method,
                reasoning_level: log.reasoning_level,
                model_reasoning_effort: log.model_reasoning_effort,
                attempt_count: log.attempt_count,
                success: log.success,
                status_code: log.status_code,
                latency_ms: log.latency_ms,
                ttfb_ms: log.ttfb_ms,
                duration_ms: log.duration_ms,
                tps: log.tps,
                prompt_tokens: log.prompt_tokens,
                completion_tokens: log.completion_tokens,
                total_tokens: log.total_tokens,
                cache_read_tokens: log.cache_read_tokens,
                cache_write_tokens: log.cache_write_tokens,
                billing_multiplier: log.billing_multiplier,
                channel_price_input_per_1k: log.channel_price_input_per_1k,
                channel_price_output_per_1k: log.channel_price_output_per_1k,
                channel_price_cache_per_1k: log.channel_price_cache_per_1k,
                prompt_cost: log.prompt_cost,
                completion_cost: log.completion_cost,
                total_cost: log.total_cost,
                billing_calculation: formatBillingCalculation(log),
                finish_reason: log.finish_reason,
                upstream_request_id: log.upstream_request_id,
                request_body_json: safeJsonParse(log.request_body_json || "") ?? log.request_body_json,
                response_body_json: safeJsonParse(log.response_body_json || "") ?? log.response_body_json,
                response_text: log.response_text,
                trace: safeJsonParse(log.trace_json || "") ?? log.trace_json,
                created_at: log.created_at,
            };
            traceContent.textContent = JSON.stringify(detail, null, 2);
            traceModal.classList.remove("hidden");
        });

        await loadFilterOptions();
        await loadLogs();
    }

    async function initConversations() {
        const listContainer = document.getElementById("conversation-list");
        const timeline = document.getElementById("conversation-timeline");
        const summary = document.getElementById("conversation-summary");
        const searchInput = document.getElementById("conversation-search");
        const refreshBtn = document.getElementById("conversation-refresh-btn");
        const openLogsLink = document.getElementById("conversation-open-logs");
        const title = document.getElementById("conversation-detail-title");
        const totalCountPrimary = document.getElementById("conversation-total-count");
        const totalCountSecondary = document.getElementById("conversation-total-count-secondary");
        const totalRequestsNode = document.getElementById("conversation-total-requests");
        const totalTokensNode = document.getElementById("conversation-total-tokens");
        const activeQueryNode = document.getElementById("conversation-active-query");
        const resultCountNode = document.getElementById("conversation-result-count");
        const lastUpdatedNode = document.getElementById("conversation-last-updated");
        const selectedProviderNode = document.getElementById("conversation-selected-provider");
        const selectedUpdatedNode = document.getElementById("conversation-selected-updated");
        const state = { items: [], activeKey: null };

        function renderConversationOverview() {
            const totalCount = state.items.length;
            const totalRequests = state.items.reduce((sum, item) => sum + Number(item.request_count || 0), 0);
            const totalTokens = state.items.reduce((sum, item) => sum + Number(item.total_tokens || 0), 0);
            const latestUpdatedAt = state.items[0]?.updated_at || null;
            const queryText = searchInput.value.trim();
            if (totalCountPrimary) totalCountPrimary.textContent = formatNumber(totalCount);
            if (totalCountSecondary) totalCountSecondary.textContent = formatNumber(totalCount);
            if (totalRequestsNode) totalRequestsNode.textContent = formatNumber(totalRequests);
            if (totalTokensNode) totalTokensNode.textContent = formatNumber(totalTokens);
            if (activeQueryNode) activeQueryNode.textContent = queryText ? `当前检索：${queryText}` : "当前展示全部会话";
            if (resultCountNode) resultCountNode.textContent = `${formatNumber(totalCount)} 条结果`;
            if (lastUpdatedNode) lastUpdatedNode.textContent = `最近更新 ${formatDate(latestUpdatedAt)}`;
        }

        async function loadConversations(preferredKey = null) {
            try {
                setButtonLoading(refreshBtn, true);
                const params = new URLSearchParams({ page: "1", page_size: "100" });
                const query = searchInput.value.trim();
                if (query) params.set("query", query);
                const data = await api.get(`/api/conversations?${params.toString()}`);
                state.items = data.items;
                renderConversationOverview();
                renderConversationList();
                const urlKey = new URLSearchParams(window.location.search).get("conversation_key");
                const nextKey = preferredKey || urlKey || state.activeKey || data.items[0]?.conversation_key || null;
                if (nextKey) {
                    await openConversation(nextKey, { replace: true });
                } else {
                    resetConversationDetail();
                }
            } catch (error) {
                showToast(error.message, "error");
            } finally {
                setButtonLoading(refreshBtn, false);
            }
        }

        function renderConversationList() {
            listContainer.innerHTML = state.items.map((item) => `
                <button class="conversation-item ${state.activeKey === item.conversation_key ? "active" : ""}" data-conversation-key="${encodeURIComponent(item.conversation_key)}">
                    <div class="conversation-item-top">
                        <strong>${escapeHtml(item.conversation_key)}</strong>
                        <span>${item.request_count} 次</span>
                    </div>
                    <div class="conversation-item-meta">
                        <span>${escapeHtml(item.latest_model || "-")}</span>
                        <span>${item.total_tokens} tokens</span>
                    </div>
                    <p>${escapeHtml(item.preview_text || "暂无回复预览")}</p>
                    <div class="conversation-item-foot">
                        <span>${formatDate(item.updated_at)}</span>
                        <span>${escapeHtml(item.latest_provider_name || "-")}</span>
                    </div>
                </button>
            `).join("") || '<div class="empty-state">暂无会话记录</div>';
            enhanceInteractiveButtons(listContainer);
        }

        function resetConversationDetail() {
            state.activeKey = null;
            title.textContent = "选择左侧会话";
            summary.innerHTML = '<div class="empty-state">选择一个会话后，可查看完整回放、命中线路与 token 使用。</div>';
            timeline.innerHTML = "";
            openLogsLink.classList.add("hidden");
            if (selectedProviderNode) selectedProviderNode.textContent = "-";
            if (selectedUpdatedNode) selectedUpdatedNode.textContent = "-";
            renderConversationList();
        }

        async function openConversation(conversationKey, { replace = false } = {}) {
            try {
                const detail = await api.get(`/api/conversations/${encodeURIComponent(conversationKey)}`);
                state.activeKey = detail.conversation_key;
                title.textContent = detail.conversation_key;
                summary.innerHTML = `
                    <div class="conversation-summary-grid">
                        <article class="conversation-summary-card">
                            <span>请求数</span>
                            <strong>${detail.request_count}</strong>
                        </article>
                        <article class="conversation-summary-card">
                            <span>成功 / 失败</span>
                            <strong>${detail.success_count} / ${detail.failure_count}</strong>
                        </article>
                        <article class="conversation-summary-card">
                            <span>总 Token</span>
                            <strong>${detail.total_tokens}</strong>
                        </article>
                        <article class="conversation-summary-card">
                            <span>最近模型</span>
                            <strong>${escapeHtml(detail.latest_model || "-")}</strong>
                        </article>
                    </div>
                    <div class="conversation-meta-bar">
                        <span>首条时间 ${formatDate(detail.started_at)}</span>
                        <span>最近更新时间 ${formatDate(detail.updated_at)}</span>
                        <span>最近命中线路 ${escapeHtml(detail.latest_provider_name || "-")}</span>
                    </div>
                `;
                timeline.innerHTML = detail.turns.map((turn) => `
                    <article class="conversation-turn conversation-turn-${escapeHtml(turn.role)}">
                        <div class="conversation-turn-head">
                            <div>
                                <span class="conversation-role">${escapeHtml(turn.role)}</span>
                                <strong>${escapeHtml(turn.provider_name || turn.requested_model || "-")}</strong>
                            </div>
                            <div class="conversation-turn-meta">
                                <span>${formatDate(turn.created_at)}</span>
                                <span>${turn.total_tokens ?? "-"} tokens</span>
                                <span>${turn.is_stream ? "stream" : "json"}</span>
                                <span>${turn.has_image ? "vision" : "text"}</span>
                            </div>
                        </div>
                        <pre class="conversation-turn-body">${escapeHtml(turn.content || "")}</pre>
                    </article>
                `).join("") || '<div class="empty-state">该会话暂无可回放内容</div>';
                openLogsLink.href = `/logs?conversation_key=${encodeURIComponent(detail.conversation_key)}`;
                openLogsLink.classList.remove("hidden");
                if (selectedProviderNode) {
                    selectedProviderNode.textContent = detail.latest_provider_name || "-";
                }
                if (selectedUpdatedNode) {
                    selectedUpdatedNode.textContent = formatDate(detail.updated_at);
                }
                renderConversationList();
                const url = `/conversations?conversation_key=${encodeURIComponent(detail.conversation_key)}`;
                if (replace) {
                    window.history.replaceState({ path: url }, "", url);
                } else {
                    window.history.pushState({ path: url }, "", url);
                }
                updateActiveNavigation("/conversations");
            } catch (error) {
                showToast(error.message, "error");
            }
        }

        listContainer.addEventListener("click", async (event) => {
            const button = event.target.closest("[data-conversation-key]");
            if (!button) return;
            await openConversation(decodeURIComponent(button.dataset.conversationKey));
        });

        searchInput.addEventListener("input", async () => {
            await loadConversations();
        });
        refreshBtn.addEventListener("click", async () => {
            try {
                setButtonLoading(refreshBtn, true);
                await loadConversations(state.activeKey);
                setButtonTransientFeedback(refreshBtn, "success", { successText: "已刷新" });
            } catch (error) {
                showToast(error.message, "error");
                setButtonTransientFeedback(refreshBtn, "error", { errorText: "刷新失败" });
            } finally {
                setButtonLoading(refreshBtn, false);
            }
        });

        await loadConversations();
    }

    function updateActiveNavigation(pathname) {
        const navLinks = Array.from(document.querySelectorAll(".nav-link[data-shell-link]"));
        let bestMatchLength = -1;
        let bestMatchPath = null;
        navLinks.forEach((link) => {
            const linkPath = new URL(link.href, window.location.origin).pathname;
            const isMatch = (linkPath === "/" && pathname === "/")
                || (linkPath !== "/" && (pathname === linkPath || pathname.startsWith(`${linkPath}/`)));
            if (isMatch && linkPath.length > bestMatchLength) {
                bestMatchLength = linkPath.length;
                bestMatchPath = linkPath;
            }
        });
        navLinks.forEach((link) => {
            const linkPath = new URL(link.href, window.location.origin).pathname;
            link.classList.toggle("active", linkPath === bestMatchPath);
        });
    }

    async function initializePage() {
        try {
            runPageCleanup();
            page = document.body.dataset.page;
            initBillingTooltipLayer();
            initProviderStatusTooltipLayer();
            enhanceInteractiveButtons(document);
            initRawApiKeyValidationControls(document);
            scheduleResponsiveTableSync(document);
            if (page === "dashboard") await initDashboard();
            if (page === "providers") await initProviders();
            if (page === "models") await initModels();
            if (page === "settings") await initSettings();
            if (page === "playground") await initPlayground();
            if (page === "users") await initUsersPage();
            if (page === "alerts") await initAlertsPage();
            if (page === "user-home") await initUserHome();
            if (page === "user-api-keys") await initUserApiKeys();
            if (page === "user-billing") await initUserBilling();
            if (page === "user-logs") await initUserLogs();
            if (page === "user-conversations") await initUserConversations();
            if (page === "user-self-test") await initUserSelfTest();
            if (page === "user-models") await initUserModels();
            if (page === "api-keys") await initApiKeys();
            if (page === "api-key-detail") await initApiKeyDetail();
            if (page === "logs") await initLogs();
            if (page === "conversations") await initConversations();
        } catch (error) {
            showToast(error.message, "error");
        }
    }

    async function navigateWithinShell(url, { replace = false } = {}) {
        const target = new URL(url, window.location.origin);
        const targetPath = `${target.pathname}${target.search}`;
        const guardRedirect = resolveRouteRedirect(targetPath);
        if (guardRedirect) {
            window.location.href = guardRedirect;
            return;
        }
        const response = await fetch(targetPath, { headers: { "X-Requested-With": "shell-nav" } });
        if (response.redirected && new URL(response.url, window.location.origin).pathname !== target.pathname) {
            window.location.href = response.url;
            return;
        }
        if (!response.ok) {
            throw new Error(`页面加载失败: ${response.status}`);
        }
        const html = await response.text();
        const doc = new DOMParser().parseFromString(html, "text/html");
        const nextContent = doc.getElementById("app-content");
        const nextTitle = doc.getElementById("topbar-title");
        if (!nextContent || !nextTitle) {
            window.location.href = target.pathname;
            return;
        }

        document.getElementById("app-content").innerHTML = nextContent.innerHTML;
        document.getElementById("topbar-title").textContent = nextTitle.textContent;
        document.body.dataset.page = doc.body.dataset.page || "";
        document.body.dataset.pageRole = doc.body.dataset.pageRole || "";
        document.title = doc.title;
        updateActiveNavigation(target.pathname);
        if (replace) {
            window.history.replaceState({ path: targetPath }, "", targetPath);
        } else {
            window.history.pushState({ path: targetPath }, "", targetPath);
        }
        scheduleResponsiveTableSync(document);
        await initializePage();
    }

    function initShellNavigation() {
        const appContent = document.getElementById("app-content");
        if (appContent) {
            const observer = new MutationObserver(() => {
                scheduleResponsiveTableSync(appContent);
            });
            observer.observe(appContent, { childList: true, subtree: true });
        }

        document.addEventListener("click", async (event) => {
            const copyButton = event.target.closest("[data-copy-text]");
            if (copyButton) {
                event.preventDefault();
                await copyText(copyButton.dataset.copyText, copyButton);
                return;
            }
        });

        document.addEventListener("click", async (event) => {
            const link = event.target.closest("a[data-shell-link]");
            if (!link) return;
            document.getElementById("site-nav-panel")?.classList.remove("is-open");
            document.getElementById("site-nav-toggle")?.classList.remove("is-open");
            document.getElementById("site-nav-toggle")?.setAttribute("aria-expanded", "false");
            const target = new URL(link.href, window.location.origin);
            if (target.origin !== window.location.origin) return;
            event.preventDefault();
            try {
                await navigateWithinShell(target.pathname);
            } catch (error) {
                showToast(error.message, "error");
                window.location.href = target.pathname;
            }
        });

        window.addEventListener("popstate", async () => {
            try {
                await navigateWithinShell(`${window.location.pathname}${window.location.search}`, { replace: true });
            } catch {
                window.location.reload();
            }
        });
    }

    document.addEventListener("DOMContentLoaded", async () => {
        const initialRedirect = resolveRouteRedirect(`${window.location.pathname}${window.location.search}`);
        if (initialRedirect) {
            window.location.replace(initialRedirect);
            return;
        }
        initThemeToggle();
        initSiteNavigation();
        initShellNavigation();
        updateActiveNavigation(window.location.pathname);
        await initializePage();
    });
})();
