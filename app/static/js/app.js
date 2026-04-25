(function () {
    let page = document.body.dataset.page;
    const authContext = {
        isAuthenticated: document.body.dataset.authenticated === "true",
        userRole: document.body.dataset.userRole || "",
    };
    const PUBLIC_ROUTE_PREFIXES = ["/login", "/register", "/setup-admin"];
    const ADMIN_ROUTE_PREFIXES = ["/providers", "/settings", "/playground", "/docs", "/api-keys", "/logs", "/conversations", "/users"];

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

    const api = {
        get: async (url) => parseResponse(await fetch(url, { cache: "no-store", headers: { "Cache-Control": "no-cache" } })),
        post: async (url, data) => parseResponse(await fetch(url, withJson("POST", data))),
        put: async (url, data) => parseResponse(await fetch(url, withJson("PUT", data))),
        delete: async (url) => parseResponse(await fetch(url, { method: "DELETE" })),
    };

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

    function safeJsonParse(text) {
        try {
            return JSON.parse(text);
        } catch {
            return null;
        }
    }

    function showToast(message, type = "success") {
        const stack = document.getElementById("toast-stack");
        if (!stack) return;
        const node = document.createElement("div");
        node.className = `toast toast-${type}`;
        node.textContent = message;
        stack.appendChild(node);
        setTimeout(() => node.remove(), 2800);
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

    function renderProviderTestModalBody(result, options = {}) {
        const scope = options.scope || "provider";
        const titleName = options.name || result?.provider_name || result?.model_name || "测试对象";
        const modelResults = Array.isArray(result?.model_results) ? result.model_results : [];
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
                    </article>
                `).join("")
                : '<div class="empty-state">当前中转站没有可展示的模型测试结果</div>')
            : "";
        return `
            <div class="provider-test-result-shell">
                <section class="provider-test-result-card">
                    <div class="panel-kicker">Test Summary</div>
                    <div class="provider-test-summary-grid">${summaryHtml}</div>
                </section>
                <section class="provider-test-result-card">
                    <div class="panel-kicker">Result Message</div>
                    <div class="provider-test-message">${messageHtml}</div>
                </section>
                ${scope === "provider" ? `
                <section class="provider-test-result-card">
                    <div class="panel-kicker">Model Results</div>
                    <div class="provider-test-model-list">${modelHtml}</div>
                </section>
                ` : ""}
            </div>
        `;
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

    function parseModelConfigs(input) {
        return input
            .split(/\r?\n/)
            .map((line) => line.trim())
            .filter(Boolean)
            .map((line) => {
                const [modelName, priority, weight, supportsStream, supportsVision, enabled, inputPrice, outputPrice] = line.split("|").map((item) => item?.trim() ?? "");
                return {
                    model_name: modelName,
                    priority: priority ? Number(priority) : 100,
                    weight: weight ? Number(weight) : 100,
                    supports_stream: !supportsStream || /^(y|yes|true|1)$/i.test(supportsStream),
                    supports_vision: /^(y|yes|true|1)$/i.test(supportsVision),
                    enabled: !enabled || /^(y|yes|true|1)$/i.test(enabled),
                    input_price_per_1k: inputPrice === "" ? null : toPricePer1K(inputPrice),
                    output_price_per_1k: outputPrice === "" ? null : toPricePer1K(outputPrice),
                };
            })
            .filter((item) => item.model_name)
            .map((item) => ({
                ...item,
                input_price_per_1k: Number.isFinite(item.input_price_per_1k) ? item.input_price_per_1k : null,
                output_price_per_1k: Number.isFinite(item.output_price_per_1k) ? item.output_price_per_1k : null,
            }));
    }

    function formatModelConfigs(modelConfigs = []) {
        return modelConfigs.map((item) => (
            `${item.model_name}|${item.priority}|${item.weight}|${item.supports_stream ? "y" : "n"}|${item.supports_vision ? "y" : "n"}|${item.enabled ? "y" : "n"}|${toPricePer1M(item.input_price_per_1k) ?? ""}|${toPricePer1M(item.output_price_per_1k) ?? ""}`
        )).join("\n");
    }

    function buildModelConfigLine(modelName) {
        const normalized = String(modelName || "").trim();
        if (!normalized) return "";
        const supportsVision = /gpt-4o|gpt-4\.1|gpt-5/i.test(normalized);
        return `${normalized}|100|100|y|${supportsVision ? "y" : "n"}|y||`;
    }

    function appendModelConfigLine(textarea, modelName) {
        if (!textarea) return false;
        const line = buildModelConfigLine(modelName);
        if (!line) return false;
        const existingConfigs = parseModelConfigs(textarea.value);
        if (existingConfigs.some((item) => item.model_name === modelName.trim())) {
            showToast(`模型 ${modelName.trim()} 已存在`, "error");
            return false;
        }
        textarea.value = textarea.value.trim()
            ? `${textarea.value.trim()}\n${line}`
            : line;
        return true;
    }

    function collectConfiguredModels(providers = [], options = {}) {
        const {
            requireStream = false,
            requireVision = false,
        } = options;
        const seen = new Set();
        const models = [];
        providers
            .filter((provider) => provider.enabled)
            .forEach((provider) => {
                (provider.model_configs || []).forEach((modelConfig) => {
                    if (!modelConfig?.enabled || !modelConfig.model_name) return;
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
        } = options;
        if (!provider?.enabled) return [];
        return (provider.model_configs || [])
            .filter((modelConfig) => (
                modelConfig?.enabled
                && modelConfig.model_name
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
        return Number(value).toFixed(6);
    }

    function formatPrice(value) {
        if (value == null || Number.isNaN(Number(value))) return "-";
        return `${toPricePer1M(value).toFixed(4)}/1M`;
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

    function renderProviderCost(provider) {
        return `
            <div>输入 ${escapeHtml(formatPrice(provider.best_input_price_per_1k))}</div>
            <div class="table-muted">输出 ${escapeHtml(formatPrice(provider.best_output_price_per_1k))}</div>
        `;
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
        if (!modeCards || !liveSummary) return;
        const defaultProviderLabel = getDefaultProviderLabel(providers, defaultProviderId);
        const hasDefaultProvider = Boolean(defaultProviderId) && providers.some((item) => item.id === Number(defaultProviderId));
        modeCards.innerHTML = buildRouteModeCards(routeMode, defaultProviderLabel, manualAllowFallback);
        liveSummary.innerHTML = buildRouteLiveSummary(routeMode, defaultProviderLabel, manualAllowFallback, hasDefaultProvider);
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
        if (!modelConfigs.length) return "-";
        return modelConfigs.map((item) => `
            <div style="display:flex; align-items:center; gap:6px; flex-wrap:wrap; margin-bottom:6px;">
                <strong>${escapeHtml(item.model_name)}</strong>
                ${statusBadge(item.health_status)}
                <span class="table-muted">P${item.priority} / W${item.weight}</span>
                <button class="table-action-btn" data-action="test-model" data-provider-id="${providerId}" data-model-id="${item.id}">测模型</button>
            </div>
        `).join("");
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

    function renderBatchConnectivityResults(results = []) {
        if (!results.length) {
            return `
                <section class="playground-result-card playground-result-error">
                    <div class="playground-card-title">批量测试结果</div>
                    <div class="playground-reply-content"><p>没有可展示的测试结果。</p></div>
                </section>
            `;
        }

        const providerTotal = results.length;
        const providerSuccess = results.filter((item) => item.success).length;
        const modelTotal = results.reduce((sum, item) => sum + (item.models_total ?? 0), 0);
        const modelSuccess = results.reduce((sum, item) => sum + (item.models_success ?? 0), 0);

        return `
            <section class="playground-result-card">
                <div class="playground-card-title">批量测试概览</div>
                <div class="playground-token-grid">
                    <div class="playground-token-item">
                        <div class="playground-token-label">渠道数</div>
                        <div class="playground-token-value">${providerSuccess}/${providerTotal}</div>
                    </div>
                    <div class="playground-token-item">
                        <div class="playground-token-label">模型数</div>
                        <div class="playground-token-value">${modelSuccess}/${modelTotal}</div>
                    </div>
                    <div class="playground-token-item">
                        <div class="playground-token-label">渠道成功</div>
                        <div class="playground-token-value">${providerSuccess}</div>
                    </div>
                    <div class="playground-token-item">
                        <div class="playground-token-label">模型成功</div>
                        <div class="playground-token-value">${modelSuccess}</div>
                    </div>
                </div>
            </section>
            ${results.map((item) => `
                <section class="playground-result-card">
                    <div class="playground-batch-card-head">
                        <div>
                            <div class="playground-card-title">${escapeHtml(item.provider_name || `渠道 ${item.provider_id}`)}</div>
                            <div class="table-muted">${item.provider_enabled ? "已启用" : "已停用"} · 模型 ${item.models_success ?? 0}/${item.models_total ?? 0} 正常</div>
                        </div>
                        <div>${statusBadge(item.health_status || "unknown")}</div>
                    </div>
                    <div class="playground-info-row">
                        <div class="playground-info-label">渠道连通</div>
                        <div class="playground-info-value">${item.provider_success ? '<span class="playground-status-success">成功</span>' : '失败'}</div>
                    </div>
                    <div class="playground-info-row">
                        <div class="playground-info-label">耗时</div>
                        <div class="playground-info-value">${item.latency_ms ?? "-"} ms</div>
                    </div>
                    <div class="playground-info-row">
                        <div class="playground-info-label">状态码</div>
                        <div class="playground-info-value">${item.status_code ?? "-"}</div>
                    </div>
                    <div class="playground-info-row">
                        <div class="playground-info-label">结果说明</div>
                        <div class="playground-info-value">${escapeHtml(item.message || "-")}</div>
                    </div>
                    <div class="playground-batch-model-list">
                        ${(item.model_results || []).length ? item.model_results.map((model) => `
                            <article class="playground-batch-model-item">
                                <div class="playground-batch-model-top">
                                    <strong>${escapeHtml(model.model_name || "-")}</strong>
                                    <div>${statusBadge(model.health_status || "unknown")}</div>
                                </div>
                                <div class="table-muted">状态码 ${model.status_code ?? "-"} · 耗时 ${model.latency_ms ?? "-"} ms</div>
                                <div class="playground-batch-model-message">${escapeHtml(model.message || "-")}</div>
                            </article>
                        `).join("") : '<div class="playground-provider-list-empty">当前渠道没有可测试模型</div>'}
                    </div>
                </section>
            `).join("")}
        `;
    }

    async function copyText(value) {
        if (!value) return;
        try {
            await navigator.clipboard.writeText(value);
            showToast("已复制");
        } catch {
            showToast("复制失败", "error");
        }
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

    async function readStreamingResponse(response, payload, meta) {
        if (!response.body) {
            throw new Error("当前浏览器不支持流式读取响应");
        }

        const context = {
            isStream: true,
            model: payload.model,
            endpointLabel: payload.endpointLabel,
            providerName: response.headers.get("X-Proxy-Provider-Name") || "-",
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
        button.disabled = isLoading;
    }

    function enhanceInteractiveButtons(scope = document) {
        scope.querySelectorAll(".interactive-btn, .table-action-btn").forEach((button) => {
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
                    await api.post("/api/providers/test-all");
                    showToast("已触发全部中转检查");
                    await refreshDashboard();
                } catch (error) {
                    showToast(error.message, "error");
                } finally {
                    setButtonLoading(checkAllBtn, false);
                }
            });
        }
        await refreshDashboard();
    }

    async function refreshDashboard() {
        const [stats, providers, settings, metrics] = await Promise.all([
            api.get("/api/dashboard"),
            api.get("/api/providers"),
            api.get("/api/settings"),
            api.get("/api/metrics/summary?window_minutes=60"),
        ]);
        document.querySelector('[data-stat="provider_count"]').textContent = stats.provider_count;
        document.querySelector('[data-stat="healthy_count"]').textContent = stats.healthy_count;
        document.querySelector('[data-stat="degraded_count"]').textContent = stats.degraded_count;
        document.querySelector('[data-stat="unhealthy_count"]').textContent = stats.unhealthy_count;
        document.querySelector('[data-stat="model_count"]').textContent = stats.model_count;
        document.querySelector('[data-stat="recent_requests"]').textContent = stats.recent_requests;
        document.querySelector('[data-stat="recent_tokens"]').textContent = stats.recent_tokens;
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
    }

    async function initProviders() {
        const tableBody = document.getElementById("provider-table-body");
        const modelTableBody = document.getElementById("provider-model-table-body");
        const modal = document.getElementById("provider-modal");
        const testResultModal = document.getElementById("provider-test-result-modal");
        const testResultModalTitle = document.getElementById("provider-test-result-modal-title");
        const testResultModalContent = document.getElementById("provider-test-result-modal-content");
        const searchInput = document.getElementById("provider-search");
        const checkAllBtn = document.getElementById("providers-check-all-btn");
        const submitBtn = document.getElementById("provider-submit-btn");
        const providerModelsTextarea = document.getElementById("provider-models");
        const customModelInput = document.getElementById("provider-custom-model-name");
        const addCustomModelBtn = document.getElementById("provider-add-custom-model");
        let providers = [];

        enhanceInteractiveButtons(document);
        document.getElementById("add-provider-btn").addEventListener("click", () => openProviderModal());
        document.getElementById("provider-modal-close").addEventListener("click", closeProviderModal);
        document.getElementById("provider-form-cancel").addEventListener("click", closeProviderModal);
        document.getElementById("provider-test-result-modal-close")?.addEventListener("click", closeTestResultModal);
        document.querySelectorAll("[data-model-preset]").forEach((button) => {
            button.addEventListener("click", () => {
                const modelName = button.dataset.modelPreset;
                if (appendModelConfigLine(providerModelsTextarea, modelName)) {
                    showToast(`已添加预设模型 ${modelName}`);
                }
            });
        });
        addCustomModelBtn.addEventListener("click", () => {
            const modelName = customModelInput.value.trim();
            if (!modelName) {
                showToast("请先输入自定义模型名", "error");
                return;
            }
            if (appendModelConfigLine(providerModelsTextarea, modelName)) {
                customModelInput.value = "";
                showToast(`已添加自定义模型 ${modelName}`);
            }
        });
        checkAllBtn.addEventListener("click", async () => {
            try {
                setButtonLoading(checkAllBtn, true);
                const results = await api.post("/api/providers/test-all");
                const successCount = results.filter((item) => item.success).length;
                showToast(`已完成全部健康检查：${successCount}/${results.length} 成功`);
                await loadProviders();
            } catch (error) {
                showToast(error.message, "error");
            } finally {
                setButtonLoading(checkAllBtn, false);
            }
        });

        modal.addEventListener("click", (event) => {
            if (event.target === modal) closeProviderModal();
        });
        testResultModal?.addEventListener("click", (event) => {
            if (event.target === testResultModal) closeTestResultModal();
        });

        document.getElementById("provider-form").addEventListener("submit", async (event) => {
            event.preventDefault();
            const id = document.getElementById("provider-id").value;
            const apiKey = document.getElementById("provider-api-key").value.trim();
            if (!id && !apiKey) {
                showToast("新增中转站时必须填写 API Key", "error");
                return;
            }
            const payload = {
                name: document.getElementById("provider-name").value.trim(),
                base_url: document.getElementById("provider-base-url").value.trim(),
                provider_type: document.getElementById("provider-type").value.trim() || "openai_compatible",
                enabled: document.getElementById("provider-enabled").checked,
                priority: Number(document.getElementById("provider-priority").value),
                weight: Number(document.getElementById("provider-weight").value),
                timeout_ms: Number(document.getElementById("provider-timeout-ms").value),
                max_retries: Number(document.getElementById("provider-max-retries").value),
                model_configs: parseModelConfigs(document.getElementById("provider-models").value),
                remark: document.getElementById("provider-remark").value.trim(),
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
                closeProviderModal();
                await loadProviders();
            } catch (error) {
                showToast(error.message, "error");
            } finally {
                setButtonLoading(submitBtn, false);
            }
        });

        searchInput.addEventListener("input", () => renderProviders(searchInput.value));

        async function loadProviders() {
            providers = await api.get("/api/providers");
            renderProviderTelemetry(providers);
            renderProviders(searchInput.value);
            renderProviderModels(searchInput.value);
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
            document.getElementById("providers-summary-card").innerHTML = `
                <div class="cockpit-aside-label">渠道脉冲</div>
                <div class="cockpit-aside-value">${summary.enabledProviderCount}</div>
                <div class="cockpit-aside-copy">当前已启用中转站</div>
                <div class="cockpit-health-bar"><span style="width:${healthyRatio}%"></span></div>
                <div class="cockpit-aside-meta">
                    <span>模型 ${summary.modelCount}</span>
                    <span>支持流式 ${summary.streamModelCount}</span>
                </div>
            `;

            document.getElementById("provider-capability-list").innerHTML = `
                <div><span>Stream 模型</span><strong>${summary.streamModelCount}</strong></div>
                <div><span>Vision 模型</span><strong>${summary.visionModelCount}</strong></div>
                <div><span>已配置单价</span><strong>${summary.pricedModelCount}</strong></div>
                <div><span>健康模型</span><strong>${summary.healthyModelCount}</strong></div>
                <div><span>异常模型</span><strong>${summary.unhealthyModelCount}</strong></div>
            `;
        }

        function renderProviders(keyword = "") {
            const query = keyword.trim().toLowerCase();
            const filtered = providers.filter((provider) => {
                if (!query) return true;
                const text = [provider.name, provider.base_url, provider.models.join(", "), provider.remark || ""].join(" ").toLowerCase();
                return text.includes(query);
            });
            tableBody.innerHTML = filtered.map((provider) => `
                <tr>
                    <td>
                        <strong>${escapeHtml(provider.name)}</strong>
                        <div class="table-muted">API Key ${escapeHtml(provider.api_key_masked)}</div>
                    </td>
                    <td>${escapeHtml(provider.base_url)}</td>
                    <td>${renderProviderModelHealth(provider.model_configs, provider.id)}</td>
                    <td>${statusBadge(provider.health_status)}</td>
                    <td>${statusBadge(provider.circuit_state)}</td>
                    <td>${renderProviderCost(provider)}</td>
                    <td>${renderQualitySummary(provider)}</td>
                    <td>${provider.priority}</td>
                    <td>${provider.weight}</td>
                    <td>
                        <div class="table-actions">
                            <button class="table-action-btn" data-action="edit" data-id="${provider.id}">编辑</button>
                            <button class="table-action-btn" data-action="test" data-id="${provider.id}">测试</button>
                            <button class="table-action-btn" data-action="default" data-id="${provider.id}">设为默认</button>
                            <button class="table-action-btn" data-action="toggle" data-id="${provider.id}">${provider.enabled ? "禁用" : "启用"}</button>
                            <button class="table-action-btn" data-action="delete" data-id="${provider.id}">删除</button>
                        </div>
                    </td>
                </tr>
            `).join("") || '<tr><td colspan="10"><div class="empty-state">没有匹配的中转站</div></td></tr>';
            enhanceInteractiveButtons(tableBody);
        }

        function renderProviderModels(keyword = "") {
            const query = keyword.trim().toLowerCase();
            const rows = providers.flatMap((provider) => provider.model_configs.map((model) => ({ provider, model })));
            const filtered = rows.filter(({ provider, model }) => {
                if (!query) return true;
                const text = [provider.name, provider.base_url, model.model_name, provider.remark || ""].join(" ").toLowerCase();
                return text.includes(query);
            });
            modelTableBody.innerHTML = filtered.map(({ provider, model }) => `
                <tr>
                    <td>${escapeHtml(provider.name)}</td>
                    <td>
                        <strong>${escapeHtml(model.model_name)}</strong>
                        <div class="table-muted">${model.last_error ? escapeHtml(model.last_error) : "-"}</div>
                    </td>
                    <td>${statusBadge(model.health_status)}</td>
                    <td>${model.supports_stream ? "流式" : "非流式"} / ${model.supports_vision ? "图像" : "文本"}</td>
                    <td>
                        <input class="field-input" type="number" step="0.0001" value="${toPricePer1M(model.input_price_per_1k) ?? ""}" placeholder="输入 /1M" data-model-field="input_price_per_1k" data-provider-id="${provider.id}" data-model-id="${model.id}">
                        <input class="field-input mt-2" type="number" step="0.0001" value="${toPricePer1M(model.output_price_per_1k) ?? ""}" placeholder="输出 /1M" data-model-field="output_price_per_1k" data-provider-id="${provider.id}" data-model-id="${model.id}">
                    </td>
                    <td>${renderQualitySummary(model)}</td>
                    <td><input class="field-input" type="number" value="${model.priority}" data-model-field="priority" data-provider-id="${provider.id}" data-model-id="${model.id}"></td>
                    <td><input class="field-input" type="number" value="${model.weight}" data-model-field="weight" data-provider-id="${provider.id}" data-model-id="${model.id}"></td>
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
            `).join("") || '<tr><td colspan="10"><div class="empty-state">没有匹配的模型</div></td></tr>';
            enhanceInteractiveButtons(modelTableBody);
        }

        tableBody.addEventListener("click", async (event) => {
            const button = event.target.closest("button[data-action]");
            if (!button) return;
            const action = button.dataset.action;
            const id = Number(button.dataset.id);
            if (action === "test-model") {
                const providerId = Number(button.dataset.providerId);
                const modelId = Number(button.dataset.modelId);
                const owner = providers.find((item) => item.id === providerId);
                const modelConfig = owner?.model_configs?.find((item) => item.id === modelId);
                if (!owner || !modelConfig) return;
                try {
                    setButtonLoading(button, true);
                    const result = await api.post(`/api/providers/${providerId}/models/${modelId}/test`, {});
                    showToast(
                        formatTestResultLabel(result, `模型 ${modelConfig.model_name}`),
                        result.success ? "success" : "error",
                    );
                    openTestResultModal(
                        `模型测试结果 · ${modelConfig.model_name}`,
                        renderProviderTestModalBody(result, { scope: "model", name: `${owner.name} / ${modelConfig.model_name}` }),
                    );
                    await loadProviders();
                } catch (error) {
                    showToast(error.message, "error");
                } finally {
                    setButtonLoading(button, false);
                }
                return;
            }
            const provider = providers.find((item) => item.id === id);
            if (!provider) return;
            try {
                if (action === "edit") openProviderModal(provider);
                if (action === "test") {
                    setButtonLoading(button, true);
                    const result = await api.post(`/api/providers/${id}/test`);
                    showToast(
                        formatTestResultLabel(result, provider.name),
                        result.success ? "success" : "error",
                    );
                    openTestResultModal(
                        `中转站测试结果 · ${provider.name}`,
                        renderProviderTestModalBody(result, { scope: "provider", name: provider.name }),
                    );
                    await loadProviders();
                }
                if (action === "toggle") {
                    setButtonLoading(button, true);
                    await api.put(`/api/providers/${id}`, { enabled: !provider.enabled });
                    showToast(`${provider.enabled ? "已禁用" : "已启用"} ${provider.name}`);
                    await loadProviders();
                }
                if (action === "delete") {
                    if (!window.confirm(`确认删除 ${provider.name} 吗？`)) return;
                    setButtonLoading(button, true);
                    await api.delete(`/api/providers/${id}`);
                    showToast("已删除中转站");
                    await loadProviders();
                }
                if (action === "default") {
                    setButtonLoading(button, true);
                    const settings = await api.get("/api/settings");
                    await api.put("/api/settings", { ...settings, default_provider_id: id });
                    showToast(`默认中转已切换为 ${provider.name}`);
                    await loadProviders();
                }
            } catch (error) {
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
            const owner = providers.find((item) => item.id === providerId);
            const modelConfig = owner?.model_configs?.find((item) => item.id === modelId);
            if (!owner || !modelConfig) return;

            if (action === "test-model") {
                try {
                    setButtonLoading(button, true);
                    const result = await api.post(`/api/providers/${providerId}/models/${modelId}/test`, {});
                    showToast(
                        formatTestResultLabel(result, `模型 ${modelConfig.model_name}`),
                        result.success ? "success" : "error",
                    );
                    openTestResultModal(
                        `模型测试结果 · ${modelConfig.model_name}`,
                        renderProviderTestModalBody(result, { scope: "model", name: `${owner.name} / ${modelConfig.model_name}` }),
                    );
                    await loadProviders();
                } catch (error) {
                    showToast(error.message, "error");
                } finally {
                    setButtonLoading(button, false);
                }
                return;
            }

            if (action === "save-model" || action === "toggle-model") {
                const priorityInput = modelTableBody.querySelector(`input[data-model-field="priority"][data-provider-id="${providerId}"][data-model-id="${modelId}"]`);
                const weightInput = modelTableBody.querySelector(`input[data-model-field="weight"][data-provider-id="${providerId}"][data-model-id="${modelId}"]`);
                const enabledInput = modelTableBody.querySelector(`input[data-model-field="enabled"][data-provider-id="${providerId}"][data-model-id="${modelId}"]`);
                const inputPriceInput = modelTableBody.querySelector(`input[data-model-field="input_price_per_1k"][data-provider-id="${providerId}"][data-model-id="${modelId}"]`);
                const outputPriceInput = modelTableBody.querySelector(`input[data-model-field="output_price_per_1k"][data-provider-id="${providerId}"][data-model-id="${modelId}"]`);
                const payload = {
                    priority: Number(priorityInput.value),
                    weight: Number(weightInput.value),
                    enabled: action === "toggle-model" ? !modelConfig.enabled : enabledInput.checked,
                    input_price_per_1k: inputPriceInput.value === "" ? null : toPricePer1K(inputPriceInput.value),
                    output_price_per_1k: outputPriceInput.value === "" ? null : toPricePer1K(outputPriceInput.value),
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

        function openProviderModal(provider) {
            document.getElementById("provider-modal-title").textContent = provider ? "编辑中转站" : "新增中转站";
            document.getElementById("provider-id").value = provider?.id ?? "";
            document.getElementById("provider-name").value = provider?.name ?? "";
            document.getElementById("provider-base-url").value = provider?.base_url ?? "";
            document.getElementById("provider-api-key").value = "";
            document.getElementById("provider-type").value = provider?.provider_type ?? "openai_compatible";
            document.getElementById("provider-priority").value = provider?.priority ?? 100;
            document.getElementById("provider-weight").value = provider?.weight ?? 100;
            document.getElementById("provider-timeout-ms").value = provider?.timeout_ms ?? 30000;
            document.getElementById("provider-max-retries").value = provider?.max_retries ?? 1;
            document.getElementById("provider-models").value = formatModelConfigs(provider?.model_configs ?? []);
            customModelInput.value = "";
            document.getElementById("provider-remark").value = provider?.remark ?? "";
            document.getElementById("provider-enabled").checked = provider?.enabled ?? true;
            modal.classList.remove("hidden");
        }

        function closeProviderModal() {
            modal.classList.add("hidden");
        }

        function openTestResultModal(title, html) {
            if (!testResultModal || !testResultModalTitle || !testResultModalContent) return;
            testResultModalTitle.textContent = title;
            testResultModalContent.innerHTML = html;
            testResultModal.classList.remove("hidden");
        }

        function closeTestResultModal() {
            if (!testResultModal || !testResultModalContent) return;
            testResultModal.classList.add("hidden");
            testResultModalContent.innerHTML = "";
        }

        await loadProviders();
    }

    async function initModels() {
        const tableBody = document.getElementById("models-table-body");
        const searchInput = document.getElementById("models-search");
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
        const inputPriceInput = document.getElementById("model-input-price");
        const outputPriceInput = document.getElementById("model-output-price");
        const speedLabelInput = document.getElementById("model-speed-label");
        const remarkInput = document.getElementById("model-remark");
        const bindingBody = document.getElementById("model-binding-body");
        if (!tableBody || !searchInput || !refreshBtn || !addBtn || !modal || !form || !bindingBody) return;

        const state = { models: [], providers: [], editingModelName: null };

        function updateSummary() {
            const summary = {
                total: state.models.length,
                enabled: state.models.filter((item) => item.enabled).length,
                boundProviders: state.models.reduce((sum, item) => sum + (item.provider_count || 0), 0),
                enabledProviders: state.models.reduce((sum, item) => sum + (item.enabled_provider_count || 0), 0),
            };
            document.querySelectorAll("[data-model-summary]").forEach((node) => {
                node.textContent = summary[node.dataset.modelSummary] ?? "0";
            });
        }

        function renderTable() {
            const query = searchInput.value.trim().toLowerCase();
            const filtered = state.models.filter((item) => {
                if (!query) return true;
                const haystack = [
                    item.model_name,
                    item.display_name || "",
                    item.speed_label || "",
                    (item.available_provider_names || []).join(" "),
                    item.remark || "",
                ].join(" ").toLowerCase();
                return haystack.includes(query);
            });
            tableBody.innerHTML = filtered.map((item) => `
                <tr>
                    <td>
                        <strong>${escapeHtml(item.display_name || item.model_name)}</strong>
                        <div class="table-muted">${escapeHtml(item.model_name)}</div>
                    </td>
                    <td>${item.enabled ? '<span class="status-badge status-healthy">已启用</span>' : '<span class="status-badge status-unknown">已停用</span>'}</td>
                    <td>
                        <div>输入 ${escapeHtml(formatPrice(item.input_price_per_1k ?? item.lowest_input_price_per_1k))}</div>
                        <div class="table-muted">输出 ${escapeHtml(formatPrice(item.output_price_per_1k ?? item.lowest_output_price_per_1k))}</div>
                    </td>
                    <td>${escapeHtml(item.speed_label || "-")}</td>
                    <td>
                        <strong>${escapeHtml(String(item.enabled_provider_count || 0))} / ${escapeHtml(String(item.provider_count || 0))}</strong>
                        <div class="table-muted">${escapeHtml((item.available_provider_names || []).join("、") || "未绑定")}</div>
                    </td>
                    <td>${item.avg_price_multiplier == null ? "-" : `${Number(item.avg_price_multiplier).toFixed(2)}x`}</td>
                    <td>
                        <div class="table-actions">
                            <button class="table-action-btn" data-action="edit" data-model-name="${escapeHtml(item.model_name)}">编辑</button>
                        </div>
                    </td>
                </tr>
            `).join("") || '<tr><td colspan="7"><div class="empty-state">暂无模型配置</div></td></tr>';
            enhanceInteractiveButtons(tableBody);
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
                <tr data-provider-id="${item.provider_id}">
                    <td><input type="checkbox" data-binding-field="bound" ${item.bound ? "checked" : ""}></td>
                    <td>
                        <strong>${escapeHtml(item.provider_name)}</strong>
                        <div class="table-muted">${escapeHtml(formatHealthStatusLabel(item.provider_health_status))}</div>
                    </td>
                    <td>${item.provider_enabled ? "已启用" : "已停用"}</td>
                    <td><input type="checkbox" data-binding-field="enabled" ${item.enabled ? "checked" : ""}></td>
                    <td><input class="field-input" type="number" min="0.0001" step="0.0001" data-binding-field="price_multiplier" value="${item.price_multiplier ?? 1}"></td>
                    <td><input class="field-input" type="number" data-binding-field="priority" value="${item.priority ?? 100}"></td>
                    <td><input class="field-input" type="number" data-binding-field="weight" value="${item.weight ?? 100}"></td>
                    <td data-binding-preview>-</td>
                </tr>
            `).join("");
            refreshBindingRows();
        }

        function refreshBindingRows() {
            const baseInput = inputPriceInput.value === "" ? null : Number(inputPriceInput.value);
            const baseOutput = outputPriceInput.value === "" ? null : Number(outputPriceInput.value);
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
                preview.innerHTML = `<div>输入 ${escapeHtml(inputPreview)}</div><div class="table-muted">输出 ${escapeHtml(outputPreview)}</div>`;
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
            inputPriceInput.value = detail?.input_price_per_1k == null ? "" : toPricePer1M(detail.input_price_per_1k);
            outputPriceInput.value = detail?.output_price_per_1k == null ? "" : toPricePer1M(detail.output_price_per_1k);
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

        function collectBindings() {
            return Array.from(bindingBody.querySelectorAll("tr")).map((row) => ({
                provider_id: Number(row.dataset.providerId),
                bound: row.querySelector('input[data-binding-field="bound"]').checked,
                enabled: row.querySelector('input[data-binding-field="enabled"]').checked,
                price_multiplier: Number(row.querySelector('input[data-binding-field="price_multiplier"]').value || 1),
                priority: Number(row.querySelector('input[data-binding-field="priority"]').value || 100),
                weight: Number(row.querySelector('input[data-binding-field="weight"]').value || 100),
            }));
        }

        async function loadData({ silent = false } = {}) {
            const [models, providers] = await Promise.all([api.get("/api/models"), api.get("/api/providers")]);
            state.models = models;
            state.providers = providers;
            updateSummary();
            renderTable();
            if (!silent) showToast("模型配置已刷新");
        }

        tableBody.addEventListener("click", async (event) => {
            const button = event.target.closest("[data-action='edit']");
            if (!button) return;
            try {
                const detail = await api.get(`/api/models/${encodeURIComponent(button.dataset.modelName)}`);
                openModal(detail);
            } catch (error) {
                showToast(error.message, "error");
            }
        });

        bindingBody.addEventListener("input", refreshBindingRows);
        bindingBody.addEventListener("change", refreshBindingRows);
        inputPriceInput.addEventListener("input", refreshBindingRows);
        outputPriceInput.addEventListener("input", refreshBindingRows);

        form.addEventListener("submit", async (event) => {
            event.preventDefault();
            const payload = {
                display_name: displayNameInput.value.trim() || null,
                enabled: enabledInput.checked,
                input_price_per_1k: inputPriceInput.value === "" ? null : toPricePer1K(Number(inputPriceInput.value)),
                output_price_per_1k: outputPriceInput.value === "" ? null : toPricePer1K(Number(outputPriceInput.value)),
                speed_label: speedLabelInput.value.trim() || null,
                remark: remarkInput.value.trim() || null,
                provider_bindings: collectBindings(),
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
                await loadData({ silent: true });
            } catch (error) {
                showToast(error.message, "error");
            } finally {
                setButtonLoading(submitBtn, false);
            }
        });

        addBtn.addEventListener("click", () => openModal());
        refreshBtn.addEventListener("click", async () => {
            try {
                await loadData();
            } catch (error) {
                showToast(error.message, "error");
            }
        });
        searchInput.addEventListener("input", renderTable);
        closeBtn.addEventListener("click", closeModal);
        cancelBtn.addEventListener("click", closeModal);
        modal.addEventListener("click", (event) => {
            if (event.target === modal) closeModal();
        });

        await loadData({ silent: true });
    }

    async function initSettings() {
        const form = document.getElementById("settings-form");
        const submitBtn = document.getElementById("settings-submit-btn");
        const providerSelect = document.getElementById("setting-default-provider-id");
        const routeModeSelect = document.getElementById("setting-route-mode");
        const manualAllowFallbackInput = document.getElementById("setting-manual-allow-fallback");
        const [providers, settings] = await Promise.all([api.get("/api/providers"), api.get("/api/settings")]);

        providerSelect.innerHTML = '<option value="">未设置</option>' + providers.map((provider) => `
            <option value="${provider.id}">${escapeHtml(provider.name)}</option>
        `).join("");

        routeModeSelect.value = settings.route_mode;
        document.getElementById("setting-default-provider-id").value = settings.default_provider_id ?? "";
        document.getElementById("setting-global-timeout-ms").value = settings.global_timeout_ms;
        document.getElementById("setting-global-max-retries").value = settings.global_max_retries;
        document.getElementById("setting-circuit-breaker-threshold").value = settings.circuit_breaker_threshold;
        document.getElementById("setting-health-check-interval-sec").value = settings.health_check_interval_sec;
        document.getElementById("setting-recovery-probe-interval-sec").value = settings.recovery_probe_interval_sec;
        document.getElementById("setting-max-logged-body-bytes").value = settings.max_logged_body_bytes;
        manualAllowFallbackInput.checked = settings.manual_allow_fallback;
        document.getElementById("setting-auto-health-check").checked = settings.auto_health_check;
        document.getElementById("setting-enable-token-logging").checked = settings.enable_token_logging;
        document.getElementById("setting-enable-payload-logging").checked = settings.enable_payload_logging;
        document.getElementById("setting-enable-stream-response-persist").checked = settings.enable_stream_response_persist;
        document.getElementById("setting-mask-sensitive-fields").checked = settings.mask_sensitive_fields;
        document.getElementById("setting-allow-public-user-registration").checked = settings.allow_public_user_registration;

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
                circuit_breaker_threshold: Number(document.getElementById("setting-circuit-breaker-threshold").value),
                auto_health_check: document.getElementById("setting-auto-health-check").checked,
                health_check_interval_sec: Number(document.getElementById("setting-health-check-interval-sec").value),
                recovery_probe_interval_sec: Number(document.getElementById("setting-recovery-probe-interval-sec").value),
                enable_token_logging: document.getElementById("setting-enable-token-logging").checked,
                enable_payload_logging: document.getElementById("setting-enable-payload-logging").checked,
                enable_stream_response_persist: document.getElementById("setting-enable-stream-response-persist").checked,
                mask_sensitive_fields: document.getElementById("setting-mask-sensitive-fields").checked,
                max_logged_body_bytes: Number(document.getElementById("setting-max-logged-body-bytes").value),
                allow_public_user_registration: document.getElementById("setting-allow-public-user-registration").checked,
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
        const batchSubmitBtn = document.getElementById("playground-batch-submit-btn");
        const batchSelectEnabledBtn = document.getElementById("playground-batch-select-enabled");
        const batchSelectAllBtn = document.getElementById("playground-batch-select-all");
        const batchClearBtn = document.getElementById("playground-batch-clear");
        let providerOptions = [];

        function updatePlaygroundImageFields() {
            const mode = imageModeSelect.value || "none";
            imageUrlField.classList.toggle("hidden", mode !== "url");
            imageFileField.classList.toggle("hidden", mode !== "upload");
            if (mode === "url") {
                imageNote.textContent = "将按标准图片链接请求发送，适合公网可访问图片地址。";
                return;
            }
            if (mode === "upload") {
                const file = imageFileInput.files?.[0];
                imageNote.textContent = file
                    ? (isLocalBrowserHost()
                        ? `当前为本地访问，将以 data URL 形式发送本地图片：${file.name}`
                        : `将先上传到当前服务，再把可访问图片地址发送给模型：${file.name}`)
                    : (isLocalBrowserHost()
                        ? "将把本地图片转换为 data URL 后发往内部接口，适合直接测试视觉模型。"
                        : "将先把本地图片上传到当前服务，再把生成的图片地址发给模型。");
                return;
            }
            imageNote.textContent = "当前仅发送文本。若切换为图片链接或本地上传，playground 会自动按所选内部接口拼装视觉请求。";
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
            if (!file) {
                throw new Error("请先选择一张本地图片");
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
            const models = selectedProvider
                ? collectProviderConfiguredModels(selectedProvider, { requireStream, requireVision })
                : collectConfiguredModels(providerOptions, { requireStream, requireVision });
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
                    : `当前没有可用于测试的已启用模型${requirementText}，请先到中转站管理中配置模型`;
                showToast(message, "error");
            }
        }

        async function loadPlaygroundModels() {
            providerOptions = await api.get("/api/providers");
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
        });

        providerSelect.addEventListener("change", () => {
            renderPlaygroundModels();
        });
        imageModeSelect.addEventListener("change", () => {
            updatePlaygroundImageFields();
            renderPlaygroundModels();
        });
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
                        providerName: response.headers.get("X-Proxy-Provider-Name") || "-",
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
                batchMeta.textContent = `批量测试完成，共 ${results.length} 个渠道`;
                showBatchRendered(renderBatchConnectivityResults(results));
                showToast("批量测试完成");
            } catch (error) {
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
        const tableBody = document.getElementById("api-key-table-body");
        const searchInput = document.getElementById("api-key-search");
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
        const defaultProviderSelect = document.getElementById("api-key-default-provider-id");
        const routeModeInput = document.getElementById("api-key-route-mode");
        const manualFallbackInput = document.getElementById("api-key-manual-allow-fallback");
        const enabledInput = document.getElementById("api-key-enabled");
        const ownerUserSelect = document.getElementById("api-key-owner-user-id");
        const expiresAtInput = document.getElementById("api-key-expires-at");
        const tokenLimitInput = document.getElementById("api-key-token-limit-total");
        const costLimitInput = document.getElementById("api-key-cost-limit-total");
        const balanceAmountInput = document.getElementById("api-key-balance-amount");
        const nameInput = document.getElementById("api-key-name");
        const generationModeInput = document.getElementById("api-key-generation-mode");
        const rawApiKeyInput = document.getElementById("api-key-raw-api-key");
        const remarkInput = document.getElementById("api-key-remark");
        const idInput = document.getElementById("api-key-id");
        const form = document.getElementById("api-key-form");
        const state = { summary: null, apiKeys: [], providers: [], users: [] };

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
            summarySignal.innerHTML = `
                <div class="cockpit-aside-label">密钥总览</div>
                <div class="cockpit-aside-value">${formatNumber(total)}</div>
                <div class="cockpit-aside-copy">当前可运维的 API 密钥总数</div>
                <div class="cockpit-health-bar"><span style="width:${activeRatio}%"></span></div>
                <div class="cockpit-aside-meta">
                    <span>启用 ${formatNumber(enabled)}</span>
                    <span>禁用 ${formatNumber(disabled)}</span>
                </div>
            `;
        }

        function getFilteredApiKeys() {
            const keyword = searchInput.value.trim().toLowerCase();
            if (!keyword) return state.apiKeys;
            return state.apiKeys.filter((item) => {
                return [
                    item.name,
                    item.remark,
                    item.owner_user_name,
                    item.key_prefix,
                    item.status,
                    item.key_masked,
                    item.raw_api_key,
                ].some((value) => String(value || "").toLowerCase().includes(keyword));
            });
        }

        function renderTable() {
            const filteredItems = getFilteredApiKeys();
            tableBody.innerHTML = filteredItems.map((item) => {
                const quota = buildApiKeyQuotaSummary(item);
                const cost = buildApiKeyCostSummary(item);
                return `
                    <tr>
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
                            <div class="table-muted">默认中转 ${escapeHtml(item.default_provider_id ? String(item.default_provider_id) : "-")} · 失败后回退 ${formatSwitchText(item.manual_allow_fallback)}</div>
                        </td>
                        <td>
                            <strong>${formatNumber(item.allowed_provider_ids.length)} 个</strong>
                            <div class="table-muted">${escapeHtml(item.allowed_providers.map((provider) => provider.name).join(", ") || "未绑定")}</div>
                        </td>
                        <td>
                            <strong>${escapeHtml(quota.summary)}</strong>
                            <div class="table-muted">剩余 ${item.remaining_tokens == null ? "无限额" : formatNumber(item.remaining_tokens)}</div>
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
            }).join("") || '<tr><td colspan="10"><div class="empty-state">暂无匹配的 API 密钥</div></td></tr>';
            enhanceInteractiveButtons(tableBody);
        }

        function refreshRawApiKeyInputState() {
            const isEditing = Boolean(idInput.value);
            if (isEditing) {
                generationModeInput.disabled = true;
                rawApiKeyInput.disabled = false;
                rawApiKeyInput.placeholder = "留空表示保持当前密钥不变；填写则替换为新密钥";
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
            balanceAmountInput.disabled = isEditing;
            balanceAmountInput.placeholder = isEditing ? "已创建密钥请到详情页做余额调整" : "留空表示不限制";
            populateDefaultProviderOptions(apiKey?.default_provider_id || null);
            renderApiKeyProviderSelector(providerSelector, state.providers, apiKey?.allowed_provider_ids || []);
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
            generationModeInput.value = "auto";
            rawApiKeyInput.value = "";
            balanceAmountInput.disabled = false;
            balanceAmountInput.placeholder = "留空表示不限制";
            renderApiKeyProviderSelector(providerSelector, state.providers, []);
            populateDefaultProviderOptions();
            populateOwnerUserOptions();
            refreshRoutePreview();
            refreshRawApiKeyInputState();
        }

        async function loadData({ silent = false } = {}) {
            const [summary, apiKeys, providers, users] = await Promise.all([
                api.get("/api/api-keys/summary"),
                api.get("/api/api-keys"),
                api.get("/api/providers"),
                api.get("/api/users/options"),
            ]);
            state.summary = summary;
            state.apiKeys = apiKeys;
            state.providers = providers;
            state.users = users;
            renderSummary(summary);
            renderTable();
            populateDefaultProviderOptions(defaultProviderSelect.value ? Number(defaultProviderSelect.value) : null);
            populateOwnerUserOptions(ownerUserSelect.value ? Number(ownerUserSelect.value) : null);
            renderApiKeyProviderSelector(providerSelector, state.providers, getSelectedProviderIds());
            refreshRoutePreview();
            if (!silent) showToast("API 密钥数据已刷新");
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
                route_mode: routeModeInput.value,
                default_provider_id: defaultProviderId,
                owner_user_id: ownerUserSelect.value === "" ? null : Number(ownerUserSelect.value),
                manual_allow_fallback: manualFallbackInput.checked,
                allowed_provider_ids: allowedProviderIds,
            };
            const rawApiKey = rawApiKeyInput.value.trim();
            if (!idInput.value) {
                payload.balance_amount = balanceAmountInput.value === "" ? null : Number(balanceAmountInput.value);
                if (generationModeInput.value === "custom") {
                    if (!rawApiKey) {
                        showToast("请填写自定义 API 密钥", "error");
                        return;
                    }
                    payload.raw_api_key = rawApiKey;
                }
            } else if (rawApiKey) {
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
                await loadData({ silent: true });
            } catch (error) {
                showToast(error.message, "error");
            } finally {
                setButtonLoading(submitBtn, false);
            }
        });

        addBtn.addEventListener("click", () => openModal());
        closeBtn.addEventListener("click", closeModal);
        cancelBtn.addEventListener("click", closeModal);
        modal.addEventListener("click", (event) => {
            if (event.target === modal) closeModal();
        });
        copyRawBtn.addEventListener("click", async () => copyText(rawValue.textContent));
        searchInput.addEventListener("input", renderTable);
        generationModeInput.addEventListener("change", refreshRawApiKeyInputState);
        routeModeInput.addEventListener("change", refreshRoutePreview);
        manualFallbackInput.addEventListener("change", refreshRoutePreview);
        defaultProviderSelect.addEventListener("change", refreshRoutePreview);
        providerSelector.addEventListener("change", refreshRoutePreview);

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
                    showToast("API 密钥已删除");
                    await loadData({ silent: true });
                } catch (error) {
                    showToast(error.message, "error");
                }
                return;
            }
            if (button.dataset.action === "enable" || button.dataset.action === "disable") {
                try {
                    await api.post(`/api/api-keys/${apiKeyId}/${button.dataset.action}`);
                    showToast(`API 密钥已${button.dataset.action === "enable" ? "启用" : "禁用"}`);
                    await loadData({ silent: true });
                } catch (error) {
                    showToast(error.message, "error");
                }
            }
        });

        await loadData({ silent: true });
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
        const lastRefreshLabel = document.getElementById("logs-last-refresh");
        const clearBtn = document.getElementById("logs-clear-btn");
        const providerSelect = document.getElementById("logs-provider-id");
        const modelSelect = document.getElementById("logs-model-name");
        const userAccountSelect = document.getElementById("logs-user-account-id");
        const apiClientKeyIdSelect = document.getElementById("logs-api-client-key-id");
        const apiClientKeyQuerySelect = document.getElementById("logs-api-client-key-query");
        const excludeHealthChecksInput = document.getElementById("logs-exclude-health-checks");
        const pageSizeSelect = document.getElementById("logs-page-size");
        const pageMeta = document.getElementById("logs-page-meta");
        const prevPageBtn = document.getElementById("logs-prev-page-btn");
        const nextPageBtn = document.getElementById("logs-next-page-btn");
        const traceModal = document.getElementById("log-trace-modal");
        const traceContent = document.getElementById("log-trace-content");
        const filterIds = [
            "logs-log-type",
            "logs-provider-id",
            "logs-model-name",
            "logs-user-account-id",
            "logs-api-client-key-id",
            "logs-api-client-key-query",
            "logs-success",
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
        if (currentParams.get("exclude_health_checks") === "false") {
            excludeHealthChecksInput.checked = false;
        }
        pageSizeSelect.value = String(state.pageSize);

        for (const id of filterIds) {
            document.getElementById(id).addEventListener("change", () => {
                state.page = 1;
                loadLogs();
            });
            document.getElementById(id).addEventListener("input", () => {
                state.page = 1;
                loadLogs();
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

        function formatCostValue(value) {
            if (value == null || Number.isNaN(Number(value))) return "-";
            return Number(value).toFixed(6);
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
            const userAccountId = userAccountSelect.value;
            const apiClientKeyId = apiClientKeyIdSelect.value;
            const apiClientKeyQuery = apiClientKeyQuerySelect.value;
            const success = document.getElementById("logs-success").value;
            const conversationKey = document.getElementById("logs-conversation-key").value.trim();
            const excludeHealthChecks = excludeHealthChecksInput.checked;
            if (logType) params.set("log_type", logType);
            if (providerId) params.set("provider_id", providerId);
            if (modelName) params.set("model_name", modelName);
            if (userAccountId) params.set("user_account_id", userAccountId);
            if (apiClientKeyId) params.set("api_client_key_id", apiClientKeyId);
            if (apiClientKeyQuery) params.set("api_client_key_query", apiClientKeyQuery);
            if (success) params.set("success", success);
            if (conversationKey) params.set("conversation_key", conversationKey);
            params.set("exclude_health_checks", excludeHealthChecks ? "true" : "false");
            params.set("_ts", Date.now().toString());
            try {
                const data = await api.get(`/api/logs?${params.toString()}`);
                renderLogPagination(data.total ?? data.items.length);
                renderLogSummary(data.summary || {});
                tableBody.innerHTML = data.items.map((log) => `
                    <tr>
                        <td>${formatDate(log.created_at)}</td>
                        <td>${escapeHtml(formatLogTypeLabel(log.log_type))}</td>
                        <td>
                            <strong>${escapeHtml(log.api_client_key_name || "-")}</strong>
                            <div class="table-muted">${escapeHtml(log.api_client_key_prefix || "-")}</div>
                            <div class="table-muted">${escapeHtml(log.user_account_name || "-")}</div>
                        </td>
                        <td>${escapeHtml(buildSessionValue(log))}</td>
                        <td>${escapeHtml(log.requested_model || log.model_name || "-")}</td>
                        <td>${escapeHtml(log.provider_name || "-")}</td>
                        <td>${escapeHtml(log.reasoning_level || "无")}</td>
                        <td>${escapeHtml(log.http_method || "-")}</td>
                        <td>${log.success ? statusBadge("healthy") : statusBadge("unhealthy")}<div class="table-muted">${log.status_code ?? "-"}</div></td>
                        <td>${formatMetricValue(log.attempt_count)}</td>
                        <td>${formatMetricValue(log.ttfb_ms, " ms")}</td>
                        <td>${formatRateValue(log.tps)}</td>
                        <td>${formatMetricValue(log.duration_ms ?? log.latency_ms, " ms")}</td>
                        <td>${formatMetricValue(log.prompt_tokens)}</td>
                        <td>${formatMetricValue(log.completion_tokens)}</td>
                        <td>${formatMetricValue(log.cache_read_tokens)}</td>
                        <td>${formatMetricValue(log.cache_write_tokens)}</td>
                        <td>${formatCostValue(log.total_cost)}</td>
                        <td>${log.billing_multiplier == null ? "-" : `${Number(log.billing_multiplier).toFixed(2)}x`}</td>
                        <td>
                            <div class="table-actions">
                                <button class="table-action-btn" data-action="show-trace" data-log-id="${log.id}">详情</button>
                                ${log.conversation_key ? `<button class="table-action-btn" data-action="open-conversation" data-conversation-key="${encodeURIComponent(log.conversation_key)}">回放</button>` : ""}
                            </div>
                        </td>
                    </tr>
                `).join("") || '<tr><td colspan="20"><div class="empty-state">暂无日志</div></td></tr>';
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
                prompt_cost: log.prompt_cost,
                completion_cost: log.completion_cost,
                total_cost: log.total_cost,
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
        const state = { items: [], activeKey: null };

        async function loadConversations(preferredKey = null) {
            try {
                setButtonLoading(refreshBtn, true);
                const params = new URLSearchParams({ page: "1", page_size: "100" });
                const query = searchInput.value.trim();
                if (query) params.set("query", query);
                const data = await api.get(`/api/conversations?${params.toString()}`);
                state.items = data.items;
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
            await loadConversations(state.activeKey);
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
            page = document.body.dataset.page;
            enhanceInteractiveButtons(document);
            if (page === "dashboard") await initDashboard();
            if (page === "providers") await initProviders();
            if (page === "models") await initModels();
            if (page === "settings") await initSettings();
            if (page === "playground") await initPlayground();
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
        document.title = doc.title;
        updateActiveNavigation(target.pathname);
        if (replace) {
            window.history.replaceState({ path: targetPath }, "", targetPath);
        } else {
            window.history.pushState({ path: targetPath }, "", targetPath);
        }
        await initializePage();
    }

    function initShellNavigation() {
        document.addEventListener("click", async (event) => {
            const copyButton = event.target.closest("[data-copy-text]");
            if (copyButton) {
                event.preventDefault();
                await copyText(copyButton.dataset.copyText);
                return;
            }
        });

        document.addEventListener("click", async (event) => {
            const link = event.target.closest("a[data-shell-link]");
            if (!link) return;
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
        initShellNavigation();
        updateActiveNavigation(window.location.pathname);
        await initializePage();
    });
})();
