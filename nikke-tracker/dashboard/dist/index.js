(() => {
  // dashboard/src/sdk.js
  var SDK = window.__HERMES_PLUGIN_SDK__;
  if (!SDK) {
    throw new Error("Hermes Plugin SDK not found (window.__HERMES_PLUGIN_SDK__)");
  }
  var React = SDK.React;
  var hooks = SDK.hooks || {};
  var useState = hooks.useState;
  var useEffect = hooks.useEffect;
  var useMemo = hooks.useMemo;
  var useRef = hooks.useRef;
  var useCallback = hooks.useCallback;
  var cn = SDK.utils && SDK.utils.cn || ((...parts) => parts.filter(Boolean).join(" "));
  var fetchJSON = SDK.fetchJSON;
  var authedFetch = SDK.authedFetch;

  // dashboard/src/api.js
  var API = "/api/plugins/nikke-tracker";
  async function call(path, init) {
    try {
      return await fetchJSON(API + path, init);
    } catch (err) {
      const error = new Error(err && err.message || "\u8BF7\u6C42\u5931\u8D25");
      error.status = err && err.status;
      error.body = err && err.body;
      throw error;
    }
  }
  var getBootstrap = () => call("/bootstrap");
  var getDetail = (name) => call("/detail?name=" + encodeURIComponent(name));
  var getTeams = () => call("/teams");
  var renderNote = () => call("/render", { method: "POST" });
  var saveGearBatch = (body) => call("/gear/batch", {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body)
  });
  var saveMeta = (body) => call("/meta", {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body)
  });
  var saveTarget = (body) => call("/target", {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body)
  });
  var uploadPortrait = (name, dataUrl) => call("/portrait/upload", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ name, data_url: dataUrl })
  });
  var addCharacter = (name) => call("/character", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ name })
  });
  var removeCharacter = (name, rev) => call(
    "/character/" + encodeURIComponent(name) + "?confirm=1" + (rev ? "&rev=" + encodeURIComponent(rev) : ""),
    { method: "DELETE" }
  );
  var fetchPortrait = (name) => call("/portrait/fetch", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ name })
  });
  var setFavorites = (favorites) => call("/state", {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ favorites })
  });
  async function getPortraitBytes(key, version) {
    const suffix = version ? "?v=" + encodeURIComponent(version) : "";
    const res = await authedFetch(API + "/portrait/" + encodeURIComponent(key) + suffix);
    if (!res.ok) throw new Error("\u5934\u50CF\u4E0D\u53EF\u7528");
    return res.blob();
  }

  // dashboard/src/util.js
  var TIER_ORDER = { T0: 0, T1: 1, T2: 2 };
  var ELEMENT_COLORS = {
    \u706B: "#ff6b4a",
    \u6C34: "#4aa8ff",
    \u7535: "#c77dff",
    \u94C1: "#c9c9c9",
    \u98CE: "#5fd08a"
  };
  function elementColor(element) {
    return ELEMENT_COLORS[element] || "#8a8f98";
  }
  function tierRank(tier) {
    return tier in TIER_ORDER ? TIER_ORDER[tier] : 9;
  }
  function percent(score) {
    if (score === null || score === void 0) return null;
    return Math.round(Number(score) * 100);
  }
  function scoreColor(score) {
    if (score === null || score === void 0) return "#9aa0a6";
    if (score >= 0.85) return "#5fd08a";
    if (score >= 0.68) return "#64d6c0";
    if (score >= 0.5) return "#f2c14e";
    return "#9aa0a6";
  }
  function sortCharacters(list, mode) {
    const copy = [...list];
    const byName = (a, b) => a.name.localeCompare(b.name, "zh-Hans-CN");
    if (mode === "name") return copy.sort(byName);
    if (mode === "scoreAsc") {
      return copy.sort(
        (a, b) => (a.score === null ? 2 : 0) - (b.score === null ? 2 : 0) || (a.score ?? 0) - (b.score ?? 0) || byName(a, b)
      );
    }
    if (mode === "scoreDesc") {
      return copy.sort(
        (a, b) => (a.score === null ? 2 : 0) - (b.score === null ? 2 : 0) || (b.score ?? 0) - (a.score ?? 0) || byName(a, b)
      );
    }
    return copy.sort(
      (a, b) => tierRank(a.tier) - tierRank(b.tier) || (b.score ?? -1) - (a.score ?? -1) || byName(a, b)
    );
  }
  function favoriteQueue(list) {
    return list.filter((c) => c.favorite && c.owned).sort(
      (a, b) => tierRank(a.tier) - tierRank(b.tier) || (a.score ?? 1) - (b.score ?? 1) || a.name.localeCompare(b.name, "zh-Hans-CN")
    );
  }
  function affixQuality(meta, affix, value) {
    const max = meta && meta[affix] && meta[affix].max;
    if (!max || !value) return 0;
    return Math.max(0, Math.min(1, Number(value) / Number(max)));
  }
  function nearestTier(meta, affix, value) {
    const tiers = meta && meta[affix] && meta[affix].tiers || [];
    if (!value || !tiers.length) return null;
    let best = 1;
    let diff = Infinity;
    tiers.forEach((tv, index) => {
      const d = Math.abs(Number(tv) - Number(value));
      if (d < diff) {
        diff = d;
        best = index + 1;
      }
    });
    return best;
  }
  function summaryFromPayload(payload) {
    const diag = payload.diag || {};
    const character = payload.character || {};
    return {
      score: diag.score ?? null,
      grade: diag.grade || "",
      effective: diag.effective ?? 0,
      target_total: diag.target_total ?? 0,
      max_total: diag.max_total ?? 0,
      core_summary: diag.core_summary || "",
      priority: diag.priority || "",
      skills: character.skills || null,
      gear_levels: character.gear_levels || null,
      favorite: Boolean(payload.favorite)
    };
  }
  function cleanAdvice(text) {
    if (!text) return "";
    return String(text).replace(/有效词条\s*(\d+)\/\d+/g, "\u6709\u6548\u8BCD\u6761 $1");
  }

  // dashboard/src/portrait.jsx
  var cache = /* @__PURE__ */ new Map();
  var inflight = /* @__PURE__ */ new Map();
  function Portrait({ name, element, portrait, className }) {
    const key = portrait && portrait.key;
    const version = portrait && portrait.version || 0;
    const cacheKey = key ? key + ":" + version : "";
    const hasCache = Boolean(portrait && portrait.has_cache);
    const [url, setUrl] = useState(() => {
      const hit = cacheKey ? cache.get(cacheKey) : null;
      return hit && hit.url ? hit.url : null;
    });
    useEffect(() => {
      let alive = true;
      if (!hasCache || !key) {
        setUrl(null);
        return void 0;
      }
      const hit = cache.get(cacheKey);
      if (hit && hit.url) {
        setUrl(hit.url);
        return void 0;
      }
      let pending = inflight.get(cacheKey);
      if (!pending) {
        pending = getPortraitBytes(key, version).then((blob) => {
          const objectUrl = URL.createObjectURL(blob);
          cache.set(cacheKey, { url: objectUrl });
          return objectUrl;
        }).finally(() => inflight.delete(cacheKey));
        inflight.set(cacheKey, pending);
      }
      pending.then((objectUrl) => {
        if (alive) setUrl(objectUrl);
      }).catch(() => {
        if (alive) setUrl(null);
      });
      return () => {
        alive = false;
      };
    }, [key, hasCache, version, cacheKey]);
    if (url) {
      return /* @__PURE__ */ React.createElement(
        "img",
        {
          className: className || "nt-portrait-img",
          src: url,
          alt: name,
          loading: "lazy",
          decoding: "async",
          draggable: false
        }
      );
    }
    const color = elementColor(element);
    return /* @__PURE__ */ React.createElement(
      "div",
      {
        className: "nt-portrait-fallback " + (className || ""),
        style: {
          background: "radial-gradient(circle at 50% 28%, " + color + "66 0%, " + color + "22 45%, #0b0f14 100%)"
        }
      },
      /* @__PURE__ */ React.createElement("span", { className: "nt-portrait-initial" }, String(name || "?").slice(0, 1))
    );
  }

  // dashboard/src/wall.jsx
  function Ring({ score, grade, size = 48, stroke = 4 }) {
    const pct = percent(score);
    if (pct === null) return null;
    const radius = (size - stroke) / 2;
    const circumference = 2 * Math.PI * radius;
    const filled = Math.max(0, Math.min(1, Number(score))) * circumference;
    return /* @__PURE__ */ React.createElement("div", { className: "nt-ring", title: "\u88C5\u5907\u6210\u578B\u5EA6 " + pct + "%" + (grade ? " \xB7 " + grade : "") }, /* @__PURE__ */ React.createElement("svg", { width: size, height: size, viewBox: "0 0 " + size + " " + size }, /* @__PURE__ */ React.createElement(
      "circle",
      {
        cx: size / 2,
        cy: size / 2,
        r: radius,
        fill: "none",
        stroke: "rgba(255,255,255,0.16)",
        strokeWidth: stroke
      }
    ), /* @__PURE__ */ React.createElement(
      "circle",
      {
        cx: size / 2,
        cy: size / 2,
        r: radius,
        fill: "none",
        stroke: scoreColor(score),
        strokeWidth: stroke,
        strokeDasharray: filled + " " + circumference,
        strokeLinecap: "round",
        transform: "rotate(-90 " + size / 2 + " " + size / 2 + ")"
      }
    )), /* @__PURE__ */ React.createElement("span", { className: "nt-ring-text" }, pct, "%"));
  }
  function TierBadge({ tier }) {
    if (!tier) return null;
    return /* @__PURE__ */ React.createElement("span", { className: "nt-tier nt-tier-" + tier, title: "\u68AF\u5EA6 " + tier }, tier);
  }
  var CharacterCard = React.memo(function CharacterCardImpl({ character, onOpen, onToggleFavorite, opening }) {
    const c = character;
    const score = percent(c.score);
    return /* @__PURE__ */ React.createElement(
      "div",
      {
        className: "nt-card" + (c.owned ? "" : " is-unowned") + (opening ? " is-opening" : ""),
        role: "button",
        tabIndex: 0,
        onClick: () => {
          if (!opening) onOpen(c.name);
        },
        onKeyDown: (event) => {
          if (event.key === "Enter" || event.key === " ") {
            event.preventDefault();
            if (!opening) onOpen(c.name);
          }
        },
        title: c.owned ? cleanAdvice(c.priority) || c.role : "\u5C1A\u672A\u52A0\u5165\u770B\u677F\uFF0C\u70B9\u51FB\u4F1A\u81EA\u52A8\u52A0\u5165\u5E76\u6253\u5F00\u8BE6\u60C5"
      },
      /* @__PURE__ */ React.createElement("div", { className: "nt-card-art" }, /* @__PURE__ */ React.createElement(
        Portrait,
        {
          name: c.name,
          element: c.element,
          portrait: c.portrait,
          className: "nt-card-portrait"
        }
      ), /* @__PURE__ */ React.createElement("div", { className: "nt-card-shade" }), /* @__PURE__ */ React.createElement(TierBadge, { tier: c.tier }), c.owned ? /* @__PURE__ */ React.createElement(
        "button",
        {
          type: "button",
          className: "nt-star" + (c.favorite ? " is-on" : ""),
          "aria-label": c.favorite ? "\u53D6\u6D88\u6536\u85CF" : "\u6536\u85CF",
          title: c.favorite ? "\u53D6\u6D88\u6536\u85CF" : "\u52A0\u5165\u517B\u6210\u961F\u5217",
          onClick: (event) => {
            event.stopPropagation();
            onToggleFavorite(c.name, !c.favorite);
          }
        },
        c.favorite ? "\u2605" : "\u2606"
      ) : null, c.owned ? /* @__PURE__ */ React.createElement(Ring, { score: c.score, grade: c.grade }) : /* @__PURE__ */ React.createElement("span", { className: "nt-unowned-badge" }, "\u70B9\u51FB\u52A0\u5165"), opening ? /* @__PURE__ */ React.createElement("span", { className: "nt-opening" }, "\u52A0\u5165\u4E2D\u2026") : null, /* @__PURE__ */ React.createElement("span", { className: "nt-element-dot", style: { background: elementColor(c.element) } })),
      /* @__PURE__ */ React.createElement("div", { className: "nt-card-body" }, /* @__PURE__ */ React.createElement("div", { className: "nt-card-name", title: c.name }, c.name), /* @__PURE__ */ React.createElement("div", { className: "nt-card-meta" }, c.element, " \xB7 ", c.class, " \xB7 ", c.burst), /* @__PURE__ */ React.createElement("div", { className: "nt-card-foot" }, c.owned ? /* @__PURE__ */ React.createElement(React.Fragment, null, /* @__PURE__ */ React.createElement("span", { className: "nt-grade nt-grade-" + (c.grade || "unknown") }, c.grade || "\u2014"), /* @__PURE__ */ React.createElement("span", { className: "nt-affix-count" }, "\u8BCD\u6761 ", c.effective)) : /* @__PURE__ */ React.createElement("span", { className: "nt-muted" }, "\u70B9\u51FB\u52A0\u5165\u5E76\u67E5\u770B")))
    );
  });
  function Queue({ characters, onOpen }) {
    const queue = favoriteQueue(characters).slice(0, 5);
    if (!queue.length) {
      return /* @__PURE__ */ React.createElement("section", { className: "nt-queue nt-queue-empty" }, /* @__PURE__ */ React.createElement("div", { className: "nt-queue-title" }, "\u517B\u6210\u961F\u5217"), /* @__PURE__ */ React.createElement("div", { className: "nt-muted" }, "\u70B9\u5361\u7247\u5DE6\u4E0A\u89D2\u7684\u661F\u6807\uFF0C\u628A\u60F3\u7EC3\u7684\u89D2\u8272\u6392\u8FDB\u961F\u5217\u3002"));
    }
    return /* @__PURE__ */ React.createElement("section", { className: "nt-queue" }, /* @__PURE__ */ React.createElement("div", { className: "nt-queue-head" }, /* @__PURE__ */ React.createElement("div", null, /* @__PURE__ */ React.createElement("div", { className: "nt-queue-title" }, "\u517B\u6210\u961F\u5217"), /* @__PURE__ */ React.createElement("div", { className: "nt-muted" }, "\u6309\u68AF\u5EA6\u548C\u88C5\u5907\u6210\u578B\u5EA6\u81EA\u52A8\u6392\u5E8F\uFF0C\u5148\u7EC3\u6700\u9700\u8981\u7684\u3002")), /* @__PURE__ */ React.createElement("span", { className: "nt-queue-count" }, queue.length, " \u540D")), /* @__PURE__ */ React.createElement("div", { className: "nt-queue-list" }, queue.map((c, index) => /* @__PURE__ */ React.createElement(
      "button",
      {
        key: c.name,
        type: "button",
        className: "nt-queue-item",
        onClick: () => onOpen(c.name)
      },
      /* @__PURE__ */ React.createElement("span", { className: "nt-queue-rank" }, index + 1),
      /* @__PURE__ */ React.createElement("span", { className: "nt-tier nt-tier-" + c.tier }, c.tier),
      /* @__PURE__ */ React.createElement("span", { className: "nt-queue-name" }, c.name),
      /* @__PURE__ */ React.createElement("span", { className: "nt-queue-score" }, percent(c.score), "%"),
      /* @__PURE__ */ React.createElement("span", { className: "nt-queue-arrow" }, "\u203A")
    ))));
  }
  function SkeletonCard() {
    return /* @__PURE__ */ React.createElement("div", { className: "nt-card nt-skeleton" }, /* @__PURE__ */ React.createElement("div", { className: "nt-skeleton-art" }), /* @__PURE__ */ React.createElement("div", { className: "nt-skeleton-line" }), /* @__PURE__ */ React.createElement("div", { className: "nt-skeleton-line short" }));
  }
  function Wall({ data, loading, error, onRetry, onOpen, openingName, onToggleFavorite, onRender, rendering }) {
    const [query, setQuery] = useState("");
    const [tier, setTier] = useState("all");
    const [element, setElement] = useState("all");
    const [clazz, setClazz] = useState("all");
    const [burst, setBurst] = useState("all");
    const [ownedOnly, setOwnedOnly] = useState(true);
    const [favOnly, setFavOnly] = useState(false);
    const [sort, setSort] = useState("tier");
    const characters = data && data.characters || [];
    const counts = useMemo(() => {
      const result = { all: characters.length, T0: 0, T1: 0, T2: 0 };
      characters.forEach((c) => {
        if (result[c.tier] !== void 0) result[c.tier] += 1;
      });
      return result;
    }, [characters]);
    const filtered = useMemo(() => {
      const needle = query.trim().toLowerCase();
      const list = characters.filter((c) => {
        if (ownedOnly && !c.owned) return false;
        if (favOnly && !c.favorite) return false;
        if (tier !== "all" && c.tier !== tier) return false;
        if (element !== "all" && c.element !== element) return false;
        if (clazz !== "all" && c.class !== clazz) return false;
        if (burst !== "all" && c.burst !== burst) return false;
        if (needle) {
          const haystack = [c.name, c.en, c.role, c.core_summary].filter(Boolean).join(" ").toLowerCase();
          if (!haystack.includes(needle)) return false;
        }
        return true;
      });
      return sortCharacters(list, sort);
    }, [characters, ownedOnly, favOnly, tier, element, clazz, burst, query, sort]);
    const resetFilters = () => {
      setQuery("");
      setTier("all");
      setElement("all");
      setClazz("all");
      setBurst("all");
      setOwnedOnly(true);
      setFavOnly(false);
      setSort("tier");
    };
    if (loading && !data) {
      return /* @__PURE__ */ React.createElement("div", { className: "nt-content" }, /* @__PURE__ */ React.createElement("div", { className: "nt-stats" }, [1, 2, 3, 4].map((n) => /* @__PURE__ */ React.createElement("div", { key: n, className: "nt-stat nt-skeleton-stat" }))), /* @__PURE__ */ React.createElement("div", { className: "nt-grid" }, Array.from({ length: 12 }).map((_, index) => /* @__PURE__ */ React.createElement(SkeletonCard, { key: index }))));
    }
    if (error && !data) {
      return /* @__PURE__ */ React.createElement("div", { className: "nt-content" }, /* @__PURE__ */ React.createElement("div", { className: "nt-error-card" }, /* @__PURE__ */ React.createElement("div", { className: "nt-error-title" }, "\u770B\u677F\u52A0\u8F7D\u5931\u8D25"), /* @__PURE__ */ React.createElement("pre", { className: "nt-error-detail" }, String(error.message || error)), /* @__PURE__ */ React.createElement("button", { type: "button", className: "nt-btn", onClick: onRetry }, "\u91CD\u8BD5")));
    }
    const owned = counts.all ? data.counts.owned : 0;
    const avg = percent(data.average_score);
    return /* @__PURE__ */ React.createElement("div", { className: "nt-content" }, /* @__PURE__ */ React.createElement("div", { className: "nt-stats" }, /* @__PURE__ */ React.createElement("div", { className: "nt-stat" }, /* @__PURE__ */ React.createElement("span", { className: "nt-stat-value" }, owned, /* @__PURE__ */ React.createElement("small", null, "/", data.counts.targets)), /* @__PURE__ */ React.createElement("span", { className: "nt-stat-label" }, "\u5DF2\u62E5\u6709")), /* @__PURE__ */ React.createElement("div", { className: "nt-stat" }, /* @__PURE__ */ React.createElement("span", { className: "nt-stat-value" }, avg === null ? "\u2014" : avg + "%"), /* @__PURE__ */ React.createElement("span", { className: "nt-stat-label" }, "\u5E73\u5747\u88C5\u5907\u6210\u578B\u5EA6")), /* @__PURE__ */ React.createElement("div", { className: "nt-stat" }, /* @__PURE__ */ React.createElement("span", { className: "nt-stat-value" }, data.counts.favorites), /* @__PURE__ */ React.createElement("span", { className: "nt-stat-label" }, "\u6536\u85CF / \u961F\u5217")), /* @__PURE__ */ React.createElement("div", { className: "nt-stat" }, /* @__PURE__ */ React.createElement("span", { className: "nt-stat-value" }, filtered.length), /* @__PURE__ */ React.createElement("span", { className: "nt-stat-label" }, "\u5F53\u524D\u663E\u793A"))), /* @__PURE__ */ React.createElement(Queue, { characters, onOpen }), /* @__PURE__ */ React.createElement("div", { className: "nt-toolbar" }, /* @__PURE__ */ React.createElement("div", { className: "nt-search" }, /* @__PURE__ */ React.createElement("span", { className: "nt-search-icon" }, "\u2315"), /* @__PURE__ */ React.createElement(
      "input",
      {
        className: "nt-input",
        value: query,
        onChange: (event) => setQuery(event.target.value),
        placeholder: "\u641C\u7D22\u89D2\u8272 / \u82F1\u6587\u540D / \u5B9A\u4F4D",
        "aria-label": "\u641C\u7D22\u89D2\u8272"
      }
    )), /* @__PURE__ */ React.createElement("div", { className: "nt-chips", role: "group", "aria-label": "\u68AF\u5EA6\u7B5B\u9009" }, [
      ["all", "\u5168\u90E8", counts.all],
      ["T0", "T0", counts.T0],
      ["T1", "T1", counts.T1],
      ["T2", "T2", counts.T2]
    ].map(([value, label, count]) => /* @__PURE__ */ React.createElement(
      "button",
      {
        key: value,
        type: "button",
        className: "nt-chip" + (tier === value ? " is-active" : ""),
        onClick: () => setTier(value)
      },
      label,
      " ",
      /* @__PURE__ */ React.createElement("span", { className: "nt-chip-count" }, count)
    ))), /* @__PURE__ */ React.createElement("select", { className: "nt-input nt-select", value: element, onChange: (event) => setElement(event.target.value), "aria-label": "\u5C5E\u6027" }, /* @__PURE__ */ React.createElement("option", { value: "all" }, "\u5168\u90E8\u5C5E\u6027"), ["\u706B", "\u6C34", "\u7535", "\u94C1", "\u98CE"].map((value) => /* @__PURE__ */ React.createElement("option", { key: value, value }, value))), /* @__PURE__ */ React.createElement("select", { className: "nt-input nt-select", value: clazz, onChange: (event) => setClazz(event.target.value), "aria-label": "\u804C\u4E1A" }, /* @__PURE__ */ React.createElement("option", { value: "all" }, "\u5168\u90E8\u804C\u4E1A"), ["\u706B\u529B\u578B", "\u8F85\u52A9\u578B", "\u9632\u5FA1\u578B"].map((value) => /* @__PURE__ */ React.createElement("option", { key: value, value }, value))), /* @__PURE__ */ React.createElement("select", { className: "nt-input nt-select", value: burst, onChange: (event) => setBurst(event.target.value), "aria-label": "\u7206\u88C2" }, /* @__PURE__ */ React.createElement("option", { value: "all" }, "\u5168\u90E8\u7206\u88C2"), ["I", "II", "III"].map((value) => /* @__PURE__ */ React.createElement("option", { key: value, value }, "\u7206\u88C2 ", value))), /* @__PURE__ */ React.createElement("select", { className: "nt-input nt-select", value: sort, onChange: (event) => setSort(event.target.value), "aria-label": "\u6392\u5E8F" }, /* @__PURE__ */ React.createElement("option", { value: "tier" }, "\u68AF\u5EA6\u4F18\u5148"), /* @__PURE__ */ React.createElement("option", { value: "scoreDesc" }, "\u6210\u578B\u5EA6 \u9AD8\u2192\u4F4E"), /* @__PURE__ */ React.createElement("option", { value: "scoreAsc" }, "\u6210\u578B\u5EA6 \u4F4E\u2192\u9AD8"), /* @__PURE__ */ React.createElement("option", { value: "name" }, "\u540D\u79F0")), /* @__PURE__ */ React.createElement("label", { className: "nt-check" }, /* @__PURE__ */ React.createElement("input", { type: "checkbox", checked: !ownedOnly, onChange: (event) => setOwnedOnly(!event.target.checked) }), "\u5168\u90E8\u56FE\u9274"), /* @__PURE__ */ React.createElement("label", { className: "nt-check" }, /* @__PURE__ */ React.createElement("input", { type: "checkbox", checked: favOnly, onChange: (event) => setFavOnly(event.target.checked) }), "\u53EA\u770B\u6536\u85CF"), /* @__PURE__ */ React.createElement("button", { type: "button", className: "nt-btn nt-btn-ghost", onClick: onRender, disabled: rendering }, rendering ? "\u6E32\u67D3\u4E2D\u2026" : "\u540C\u6B65 nikke.md")), data.warnings && data.warnings.length > 0 ? /* @__PURE__ */ React.createElement("div", { className: "nt-warn-strip" }, "\u6709 ", data.warnings.length, " \u4E2A\u8FDB\u5EA6\u89D2\u8272\u5728\u76EE\u6807\u5E93\u4E2D\u627E\u4E0D\u5230\uFF0C\u5DF2\u8DF3\u8FC7\uFF1A", data.warnings.map((w) => w.name).join("\u3001")) : null, filtered.length === 0 ? /* @__PURE__ */ React.createElement("div", { className: "nt-empty" }, /* @__PURE__ */ React.createElement("div", { className: "nt-empty-title" }, "\u6CA1\u6709\u5339\u914D\u7684\u89D2\u8272"), /* @__PURE__ */ React.createElement("div", { className: "nt-muted" }, "\u6362\u4E2A\u7B5B\u9009\u6761\u4EF6\uFF0C\u6216\u628A\u300C\u5168\u90E8\u56FE\u9274\u300D\u6253\u5F00\u3002"), /* @__PURE__ */ React.createElement("button", { type: "button", className: "nt-btn", onClick: resetFilters }, "\u6E05\u7A7A\u7B5B\u9009")) : /* @__PURE__ */ React.createElement("div", { className: "nt-grid" }, filtered.map((character) => /* @__PURE__ */ React.createElement(
      CharacterCard,
      {
        key: character.name,
        character,
        onOpen,
        opening: openingName === character.name,
        onToggleFavorite
      }
    ))), /* @__PURE__ */ React.createElement("div", { className: "nt-footnote" }, "\u8BCD\u6761\u767E\u5206\u6BD4\u7684\u6EE1\u503C\u4E0A\u9650\u89C1 ", /* @__PURE__ */ React.createElement("span", { className: "nt-mono" }, "references/affix_tiers.json"), "\uFF1B\u88C5\u5907\u5F3A\u5316\u7B49\u7EA7\u53EA\u5F71\u54CD\u767D\u503C\uFF0C\u4E0D\u53C2\u4E0E\u6D17\u6761\u5EFA\u8BAE\u3002"));
  }

  // dashboard/src/gear.jsx
  var SLOT_LABELS = { head: "\u5934\u90E8", body: "\u8EAB\u8EAF", arm: "\u81C2\u90E8", leg: "\u817F\u90E8" };
  var SLOT_KEYS = ["head", "body", "arm", "leg"];
  function normalisePiece(piece) {
    const slots = (piece && piece.slots || []).slice(0, 3).map((row) => ({
      affix: row && row.affix || "",
      value: Number(row && row.value || 0),
      locked: Boolean(row && row.locked)
    }));
    while (slots.length < 3) slots.push({ affix: "", value: 0, locked: false });
    return {
      level: Math.max(0, Math.min(5, Number(piece && piece.level || 0))),
      slots
    };
  }
  function normaliseGear(gear) {
    const out = {};
    SLOT_KEYS.forEach((slot) => {
      out[slot] = normalisePiece(gear && gear[slot]);
    });
    return out;
  }
  function samePiece(a, b) {
    return JSON.stringify(a) === JSON.stringify(b);
  }
  function pieceErrors(slot, piece, affixMeta) {
    return piece.slots.map((row, index) => {
      const label = SLOT_LABELS[slot] + "\u69FD" + (index + 1);
      const meta = affixMeta[row.affix] || {};
      const value = Number(row.value || 0);
      if (!row.affix && value > 0) return label + " \u6CA1\u6709\u8BCD\u6761\u65F6\u6570\u503C\u5FC5\u987B\u4E3A 0";
      if (row.affix && value <= 0) return label + " \u6570\u503C\u5FC5\u987B\u5927\u4E8E 0";
      if (row.affix && meta.max && value > Number(meta.max) + 0.01) {
        return label + " \u8D85\u8FC7\u4E0A\u9650 " + meta.max;
      }
      return "";
    });
  }
  function TierSelect({ affix, value, meta, onChange, disabled }) {
    const tiers = meta && meta.tiers || [];
    if (!tiers.length) {
      return /* @__PURE__ */ React.createElement(
        "input",
        {
          className: "nt-input nt-value-input",
          type: "number",
          step: "0.01",
          min: "0",
          value,
          disabled,
          onChange: (event) => onChange(event.target.value === "" ? 0 : Number(event.target.value)),
          "aria-label": "\u8BCD\u6761\u6570\u503C"
        }
      );
    }
    const tier = nearestTier({ [affix]: meta }, affix, value);
    const exact = tier !== null && Math.abs(Number(tiers[tier - 1]) - Number(value)) <= 5e-3;
    const current = !value ? "" : exact ? String(tier) : "custom";
    return /* @__PURE__ */ React.createElement(
      "select",
      {
        className: "nt-input nt-tier-select",
        value: current,
        disabled,
        onChange: (event) => {
          const raw = event.target.value;
          if (raw === "custom" || raw === "") return;
          onChange(Number(tiers[Number(raw) - 1]));
        },
        "aria-label": "\u8BCD\u6761\u9636\u6570"
      },
      !value ? /* @__PURE__ */ React.createElement("option", { value: "" }, "\u2014") : null,
      tiers.map((tierValue, index) => /* @__PURE__ */ React.createElement("option", { key: index, value: String(index + 1) }, index + 1, "\u9636 \xB7 ", tierValue, "%")),
      value > 0 && !exact ? /* @__PURE__ */ React.createElement("option", { value: "custom" }, "\u81EA\u5B9A\u4E49 ", value, "%") : null
    );
  }
  function PieceEditor({
    slot,
    piece,
    score,
    error,
    mainRecommended,
    subRecommended,
    affixMeta,
    affixOptions,
    onLevel,
    onRow,
    onAffix,
    disabled
  }) {
    return /* @__PURE__ */ React.createElement("section", { className: "nt-piece" }, /* @__PURE__ */ React.createElement("header", { className: "nt-piece-head" }, /* @__PURE__ */ React.createElement("div", null, /* @__PURE__ */ React.createElement("h3", null, SLOT_LABELS[slot]), /* @__PURE__ */ React.createElement("span", { className: "nt-muted" }, "Lv", piece.level, score === null || score === void 0 ? "" : " \xB7 \u5F53\u524D " + Math.round(score * 100) + "%")), /* @__PURE__ */ React.createElement("div", { className: "nt-level" }, /* @__PURE__ */ React.createElement(
      "input",
      {
        className: "nt-input nt-level-input",
        type: "number",
        min: "0",
        max: "5",
        value: piece.level,
        disabled,
        onChange: (event) => onLevel(Math.max(0, Math.min(5, Number(event.target.value || 0)))),
        "aria-label": "\u88C5\u5907\u5F3A\u5316\u7B49\u7EA7"
      }
    ))), /* @__PURE__ */ React.createElement("div", { className: "nt-affix-list" }, piece.slots.map((row, index) => {
      const meta = affixMeta[row.affix] || {};
      const quality = affixQuality(affixMeta, row.affix, row.value);
      const isMain = mainRecommended.has(row.affix);
      const isSub = subRecommended.has(row.affix);
      return /* @__PURE__ */ React.createElement("div", { className: "nt-affix-row", key: index }, /* @__PURE__ */ React.createElement("div", { className: "nt-affix-kind" }, /* @__PURE__ */ React.createElement(
        "select",
        {
          className: "nt-input nt-affix-select",
          value: row.affix || "",
          disabled,
          onChange: (event) => onAffix(index, event.target.value),
          "aria-label": "\u69FD" + (index + 1) + " \u8BCD\u6761\u7C7B\u578B"
        },
        /* @__PURE__ */ React.createElement("option", { value: "" }, "\u65E0\u8BCD\u6761"),
        affixOptions.map((affix) => /* @__PURE__ */ React.createElement("option", { key: affix, value: affix }, affix))
      )), /* @__PURE__ */ React.createElement("div", { className: "nt-value-wrap" + (row.affix ? "" : " is-disabled") }, /* @__PURE__ */ React.createElement(
        TierSelect,
        {
          affix: row.affix,
          value: row.value,
          meta,
          disabled: disabled || !row.affix,
          onChange: (value) => onRow(index, { value })
        }
      )), /* @__PURE__ */ React.createElement(
        "button",
        {
          type: "button",
          className: "nt-lock" + (row.locked ? " is-on" : ""),
          onClick: () => onRow(index, { locked: !row.locked }),
          disabled,
          title: row.locked ? "\u5DF2\u9501\u5B9A\uFF0C\u70B9\u51FB\u89E3\u9501" : "\u672A\u9501\u5B9A\uFF0C\u70B9\u51FB\u9501\u5B9A",
          "aria-label": row.locked ? "\u89E3\u9501" : "\u9501\u5B9A"
        },
        row.locked ? "\u{1F512}" : "\u{1F513}"
      ), /* @__PURE__ */ React.createElement("div", { className: "nt-affix-live" }, row.affix ? /* @__PURE__ */ React.createElement(React.Fragment, null, /* @__PURE__ */ React.createElement("span", { className: "nt-quality-track" }, /* @__PURE__ */ React.createElement(
        "span",
        {
          style: {
            width: Math.round(quality * 100) + "%",
            background: quality >= 0.8 ? "#5fd08a" : quality >= 0.55 ? "#f2c14e" : "#ff7a6b"
          }
        }
      )), isMain ? /* @__PURE__ */ React.createElement("span", { className: "nt-reco" }, "\u4E3B") : null, isSub ? /* @__PURE__ */ React.createElement("span", { className: "nt-reco nt-reco-sub" }, "\u526F") : null) : /* @__PURE__ */ React.createElement("span", { className: "nt-muted" }, "\u7A7A\u69FD")));
    })), error ? /* @__PURE__ */ React.createElement("div", { className: "nt-inline-error" }, error) : null);
  }
  function GearBoard({ gear, diag, target, affixMeta, affixOptions, serverRev, onSave, disabled }) {
    const [drafts, setDrafts] = useState(() => normaliseGear(gear));
    const [saving, setSaving] = useState(false);
    const [error, setError] = useState("");
    const baseline = useMemo(() => normaliseGear(gear), [JSON.stringify(gear || {})]);
    const dirtyRef = useRef(false);
    const scores = useMemo(() => {
      const out = {};
      (diag && diag.pieces || []).forEach((piece) => {
        out[piece.slot] = piece.score;
      });
      return out;
    }, [diag]);
    const mainRecommended = useMemo(
      () => new Set(
        (target && target.requirement && target.requirement.affixes || []).map((item) => item.affix)
      ),
      [target]
    );
    const subRecommended = useMemo(
      () => new Set(
        (target && target.requirement && target.requirement.sub_affixes || []).map((item) => item.affix)
      ),
      [target]
    );
    const dirtySlots = SLOT_KEYS.filter((slot) => !samePiece(drafts[slot], baseline[slot]));
    const dirty = dirtySlots.length > 0;
    dirtyRef.current = dirty;
    const errors = useMemo(() => {
      const bySlot = {};
      let first = "";
      SLOT_KEYS.forEach((slot) => {
        const list = pieceErrors(slot, drafts[slot], affixMeta);
        bySlot[slot] = list;
        if (!first) first = list.find(Boolean) || "";
      });
      return { bySlot, first };
    }, [drafts, affixMeta]);
    useEffect(() => {
      if (!dirtyRef.current) setDrafts(normaliseGear(gear));
    }, [serverRev]);
    const patchPiece = (slot, patch) => {
      setDrafts((previous) => ({ ...previous, [slot]: { ...previous[slot], ...patch } }));
    };
    const patchRow = (slot, index, patch) => {
      setDrafts((previous) => {
        const piece = previous[slot];
        return {
          ...previous,
          [slot]: {
            ...piece,
            slots: piece.slots.map(
              (row, rowIndex) => rowIndex === index ? { ...row, ...patch } : row
            )
          }
        };
      });
    };
    const changeAffix = (slot, index, affix) => {
      const tiers = affixMeta[affix] && affixMeta[affix].tiers || [];
      patchRow(slot, index, { affix, value: affix ? tiers[0] || 0 : 0 });
    };
    const reset = () => {
      setDrafts(normaliseGear(gear));
      setError("");
    };
    const submit = async () => {
      if (!dirty || errors.first || saving || disabled) return;
      setSaving(true);
      setError("");
      try {
        await onSave(drafts);
      } catch (err) {
        if (err && err.message) setError(err.message);
      } finally {
        setSaving(false);
      }
    };
    return /* @__PURE__ */ React.createElement("div", { className: "nt-gear" }, /* @__PURE__ */ React.createElement("div", { className: "nt-gear-bar" }, /* @__PURE__ */ React.createElement("div", { className: "nt-gear-state" }, /* @__PURE__ */ React.createElement("strong", null, "\u88C5\u5907\u8BCD\u6761"), /* @__PURE__ */ React.createElement("span", { className: "nt-muted" }, dirty ? dirtySlots.length + " \u4EF6\u672A\u4FDD\u5B58" : "\u5DF2\u4FDD\u5B58")), /* @__PURE__ */ React.createElement("div", { className: "nt-gear-actions" }, /* @__PURE__ */ React.createElement(
      "button",
      {
        type: "button",
        className: "nt-btn nt-btn-ghost",
        onClick: reset,
        disabled: !dirty || saving
      },
      "\u64A4\u9500"
    ), /* @__PURE__ */ React.createElement(
      "button",
      {
        type: "button",
        className: "nt-btn",
        onClick: submit,
        disabled: !dirty || Boolean(errors.first) || saving || disabled
      },
      saving ? "\u4FDD\u5B58\u4E2D\u2026" : "\u4FDD\u5B58\u56DB\u4EF6"
    ))), error ? /* @__PURE__ */ React.createElement("div", { className: "nt-inline-error" }, error) : null, /* @__PURE__ */ React.createElement("div", { className: "nt-pieces" }, SLOT_KEYS.map((slot) => /* @__PURE__ */ React.createElement(
      PieceEditor,
      {
        key: slot,
        slot,
        piece: drafts[slot],
        score: scores[slot],
        error: errors.bySlot[slot].find(Boolean) || "",
        mainRecommended,
        subRecommended,
        affixMeta,
        affixOptions,
        onLevel: (level) => patchPiece(slot, { level }),
        onRow: (index, patch) => patchRow(slot, index, patch),
        onAffix: (index, affix) => changeAffix(slot, index, affix),
        disabled: disabled || saving
      }
    ))));
  }

  // dashboard/src/detail.jsx
  function BigRing({ score, grade }) {
    const pct = percent(score);
    if (pct === null) return null;
    const size = 116;
    const stroke = 8;
    const radius = (size - stroke) / 2;
    const circumference = 2 * Math.PI * radius;
    const filled = Math.max(0, Math.min(1, Number(score))) * circumference;
    return /* @__PURE__ */ React.createElement("div", { className: "nt-big-ring", title: "\u6309\u56DB\u90E8\u4F4D\u88C5\u5907\u8BCD\u6761\u8BA1\u7B97" }, /* @__PURE__ */ React.createElement("svg", { width: size, height: size, viewBox: "0 0 " + size + " " + size }, /* @__PURE__ */ React.createElement("circle", { cx: size / 2, cy: size / 2, r: radius, fill: "none", stroke: "rgba(255,255,255,0.14)", strokeWidth: stroke }), /* @__PURE__ */ React.createElement(
      "circle",
      {
        cx: size / 2,
        cy: size / 2,
        r: radius,
        fill: "none",
        stroke: scoreColor(score),
        strokeWidth: stroke,
        strokeDasharray: filled + " " + circumference,
        strokeLinecap: "round",
        transform: "rotate(-90 " + size / 2 + " " + size / 2 + ")"
      }
    )), /* @__PURE__ */ React.createElement("div", { className: "nt-big-ring-text" }, /* @__PURE__ */ React.createElement("strong", null, pct, "%"), /* @__PURE__ */ React.createElement("span", null, grade || "\u2014")));
  }
  function TierBadge2({ tier }) {
    if (!tier) return null;
    return /* @__PURE__ */ React.createElement("span", { className: "nt-tier nt-tier-" + tier }, tier);
  }
  function pieceScore(piece) {
    return Math.round((piece && piece.score ? piece.score : 0) * 100);
  }
  var DETAIL_CACHE = /* @__PURE__ */ new Map();
  function storeDetail(payload) {
    if (payload && payload.character && payload.character.name) {
      DETAIL_CACHE.set(payload.character.name, payload);
    }
  }
  function cachedDetail(name, bootstrap) {
    const hit = DETAIL_CACHE.get(name);
    if (!hit) return null;
    if (bootstrap && (hit.rev !== bootstrap.rev || hit.targets_rev !== bootstrap.targets_rev)) {
      return null;
    }
    return hit;
  }
  function MetaEditor({ character, target, onSave, disabled }) {
    const [draft, setDraft] = useState(() => ({
      skills: (character.skills || [1, 1, 1]).slice(0, 3),
      collectible: character.collectible || "",
      notes: character.notes || ""
    }));
    const [saving, setSaving] = useState(false);
    const [error, setError] = useState("");
    useEffect(() => {
      setDraft({
        skills: (character.skills || [1, 1, 1]).slice(0, 3),
        collectible: character.collectible || "",
        notes: character.notes || ""
      });
      setError("");
    }, [JSON.stringify(character.skills), character.collectible, character.notes]);
    const dirty = JSON.stringify(draft.skills) !== JSON.stringify((character.skills || [1, 1, 1]).slice(0, 3)) || draft.collectible !== (character.collectible || "") || draft.notes !== (character.notes || "");
    const setSkill = (index, value) => {
      const next = [...draft.skills];
      next[index] = Math.max(1, Math.min(10, Number(value || 1)));
      setDraft({ ...draft, skills: next });
    };
    const submit = async () => {
      setSaving(true);
      setError("");
      try {
        await onSave(draft);
      } catch (err) {
        if (err && err.message) setError(err.message);
      } finally {
        setSaving(false);
      }
    };
    return /* @__PURE__ */ React.createElement("section", { className: "nt-panel nt-meta" }, /* @__PURE__ */ React.createElement("div", { className: "nt-panel-head" }, /* @__PURE__ */ React.createElement("h3", null, "\u6280\u80FD / \u6536\u85CF\u54C1"), /* @__PURE__ */ React.createElement("span", { className: "nt-muted" }, "\u76EE\u6807\u6280\u80FD ", (target.skills || []).join("/"))), /* @__PURE__ */ React.createElement("div", { className: "nt-meta-grid" }, /* @__PURE__ */ React.createElement("label", { className: "nt-field" }, /* @__PURE__ */ React.createElement("span", null, "\u6280\u80FD 1"), /* @__PURE__ */ React.createElement("input", { className: "nt-input", type: "number", min: "1", max: "10", value: draft.skills[0], onChange: (event) => setSkill(0, event.target.value) })), /* @__PURE__ */ React.createElement("label", { className: "nt-field" }, /* @__PURE__ */ React.createElement("span", null, "\u6280\u80FD 2"), /* @__PURE__ */ React.createElement("input", { className: "nt-input", type: "number", min: "1", max: "10", value: draft.skills[1], onChange: (event) => setSkill(1, event.target.value) })), /* @__PURE__ */ React.createElement("label", { className: "nt-field" }, /* @__PURE__ */ React.createElement("span", null, "\u7206\u88C2"), /* @__PURE__ */ React.createElement("input", { className: "nt-input", type: "number", min: "1", max: "10", value: draft.skills[2], onChange: (event) => setSkill(2, event.target.value) })), /* @__PURE__ */ React.createElement("label", { className: "nt-field" }, /* @__PURE__ */ React.createElement("span", null, "\u6536\u85CF\u54C1"), /* @__PURE__ */ React.createElement("input", { className: "nt-input", value: draft.collectible, onChange: (event) => setDraft({ ...draft, collectible: event.target.value }), placeholder: target.collectible || "\u672A\u8BB0\u5F55" }))), /* @__PURE__ */ React.createElement("label", { className: "nt-field nt-field-wide" }, /* @__PURE__ */ React.createElement("span", null, "\u5907\u6CE8"), /* @__PURE__ */ React.createElement("textarea", { className: "nt-input nt-textarea", value: draft.notes, onChange: (event) => setDraft({ ...draft, notes: event.target.value }), rows: 2, placeholder: "\u4F8B\u5982\uFF1A\u5148\u4E0D\u51B2 12 \u8BCD\u6761" })), error ? /* @__PURE__ */ React.createElement("div", { className: "nt-inline-error" }, error) : null, /* @__PURE__ */ React.createElement("div", { className: "nt-panel-foot" }, /* @__PURE__ */ React.createElement("span", { className: "nt-muted" }, dirty ? "\u6709\u672A\u4FDD\u5B58\u4FEE\u6539" : "\u5DF2\u4FDD\u5B58"), /* @__PURE__ */ React.createElement("button", { type: "button", className: "nt-btn", onClick: submit, disabled: !dirty || saving || disabled }, saving ? "\u4FDD\u5B58\u4E2D\u2026" : "\u4FDD\u5B58\u4FE1\u606F")));
  }
  function AdvicePanel({ diag }) {
    if (!diag) return null;
    const total = (diag.pieces || []).reduce((sum, piece) => sum + (piece.remaining_cost || 0), 0);
    return /* @__PURE__ */ React.createElement("section", { className: "nt-panel nt-advice" }, /* @__PURE__ */ React.createElement("div", { className: "nt-panel-head" }, /* @__PURE__ */ React.createElement("h3", null, "\u4E0B\u4E00\u6B65\u5EFA\u8BAE"), /* @__PURE__ */ React.createElement("span", { className: "nt-muted" }, "\u9884\u8BA1\u5408\u8BA1 ", total.toFixed(1), " \u77F3\u5934\uFF08\u6309\u6BCF\u4EF6\u72EC\u7ACB\u4F30\u7B97\uFF09")), /* @__PURE__ */ React.createElement("div", { className: "nt-actions-list" }, (diag.actions || []).map((action, index) => /* @__PURE__ */ React.createElement("div", { className: "nt-action", key: index }, /* @__PURE__ */ React.createElement("span", { className: "nt-action-index" }, index + 1), /* @__PURE__ */ React.createElement("span", null, cleanAdvice(action))))), /* @__PURE__ */ React.createElement("div", { className: "nt-piece-advice" }, (diag.pieces || []).map((piece) => /* @__PURE__ */ React.createElement("details", { className: "nt-advice-piece", key: piece.slot }, /* @__PURE__ */ React.createElement("summary", null, /* @__PURE__ */ React.createElement("span", null, piece.label), /* @__PURE__ */ React.createElement("span", { className: "nt-advice-score" }, pieceScore(piece), "%"), piece.bad && piece.bad.length ? /* @__PURE__ */ React.createElement("span", { className: "nt-advice-gap" }, "\u7F3A\u53E3 ", piece.bad.length) : null, piece.remaining_cost ? /* @__PURE__ */ React.createElement("span", { className: "nt-advice-cost" }, piece.remaining_cost, " \u77F3\u5934") : null), (piece.issues || []).map((issue, index) => /* @__PURE__ */ React.createElement("div", { key: "i" + index, className: "nt-issue nt-issue-" + issue.severity }, issue.text)), (piece.steps || []).map((step, index) => /* @__PURE__ */ React.createElement("div", { key: "s" + index, className: "nt-step" }, "\xB7 ", step)), !(piece.issues || []).length && !(piece.steps || []).length ? /* @__PURE__ */ React.createElement("div", { className: "nt-muted" }, "\u6682\u65E0\u95EE\u9898") : null))));
  }
  function TargetEditor({ target, affixOptions, onSave, onCancel, saving }) {
    const requirement = target.requirement || {};
    const [tier, setTier] = useState(target.tier || "T2");
    const [skills, setSkills] = useState((target.skills || [10, 10, 10]).slice(0, 3));
    const [main, setMain] = useState((requirement.affixes || []).map((item) => item.affix));
    const [sub, setSub] = useState((requirement.sub_affixes || []).map((item) => item.affix));
    const [affixNote, setAffixNote] = useState(requirement.text || "");
    const [note, setNote] = useState(target.note || "");
    const setSkill = (index, value) => {
      const next = [...skills];
      next[index] = Math.max(1, Math.min(10, Number(value || 1)));
      setSkills(next);
    };
    const toggleMain = (affix) => {
      setMain(
        (previous) => previous.includes(affix) ? previous.filter((item) => item !== affix) : [...previous, affix]
      );
      setSub((previous) => previous.filter((item) => item !== affix));
    };
    const toggleSub = (affix) => {
      if (main.includes(affix)) return;
      setSub(
        (previous) => previous.includes(affix) ? previous.filter((item) => item !== affix) : [...previous, affix]
      );
    };
    return /* @__PURE__ */ React.createElement("section", { className: "nt-panel nt-target-editor" }, /* @__PURE__ */ React.createElement("div", { className: "nt-panel-head" }, /* @__PURE__ */ React.createElement("h3", null, "\u6821\u6838\u63A8\u8350\u517B\u6210"), /* @__PURE__ */ React.createElement("span", { className: "nt-muted" }, "\u4FDD\u5B58\u540E\u5361\u7247\u6210\u578B\u5EA6\u4F1A\u6309\u65B0\u76EE\u6807\u91CD\u7B97")), /* @__PURE__ */ React.createElement("div", { className: "nt-edit-grid" }, /* @__PURE__ */ React.createElement("label", { className: "nt-field" }, /* @__PURE__ */ React.createElement("span", null, "\u68AF\u5EA6"), /* @__PURE__ */ React.createElement("select", { className: "nt-input", value: tier, onChange: (event) => setTier(event.target.value) }, ["T0", "T1", "T2"].map((value) => /* @__PURE__ */ React.createElement("option", { key: value, value }, value)))), ["\u6280\u80FD1", "\u6280\u80FD2", "\u7206\u88C2"].map((label, index) => /* @__PURE__ */ React.createElement("label", { className: "nt-field", key: label }, /* @__PURE__ */ React.createElement("span", null, label), /* @__PURE__ */ React.createElement(
      "input",
      {
        className: "nt-input",
        type: "number",
        min: "1",
        max: "10",
        value: skills[index],
        onChange: (event) => setSkill(index, event.target.value)
      }
    )))), /* @__PURE__ */ React.createElement("div", { className: "nt-edit-section" }, /* @__PURE__ */ React.createElement("div", { className: "nt-edit-label" }, "\u4E3B\u8BCD\u6761\uFF08\u4F18\u5148\u5237\uFF09"), /* @__PURE__ */ React.createElement("div", { className: "nt-pick-list" }, affixOptions.map((affix) => /* @__PURE__ */ React.createElement(
      "button",
      {
        key: affix,
        type: "button",
        className: "nt-pick" + (main.includes(affix) ? " is-main" : ""),
        onClick: () => toggleMain(affix)
      },
      affix
    )))), /* @__PURE__ */ React.createElement("div", { className: "nt-edit-section" }, /* @__PURE__ */ React.createElement("div", { className: "nt-edit-label" }, "\u526F\u8BCD\u6761\uFF08\u6709\u66F4\u597D\uFF09"), /* @__PURE__ */ React.createElement("div", { className: "nt-pick-list" }, affixOptions.map((affix) => /* @__PURE__ */ React.createElement(
      "button",
      {
        key: affix,
        type: "button",
        className: "nt-pick" + (sub.includes(affix) ? " is-sub" : ""),
        disabled: main.includes(affix),
        onClick: () => toggleSub(affix)
      },
      affix
    )))), /* @__PURE__ */ React.createElement("label", { className: "nt-field nt-field-wide" }, /* @__PURE__ */ React.createElement("span", null, "\u6821\u6838\u8BF4\u660E"), /* @__PURE__ */ React.createElement(
      "input",
      {
        className: "nt-input",
        value: affixNote,
        onChange: (event) => setAffixNote(event.target.value),
        placeholder: "\u4F8B\u5982\uFF1A\u4E3B: \u88C5\u5F391+ | \u526F: \u4F18\u8D8A/\u653B\u51FB"
      }
    )), /* @__PURE__ */ React.createElement("label", { className: "nt-field nt-field-wide" }, /* @__PURE__ */ React.createElement("span", null, "\u6765\u6E90 / \u5907\u6CE8"), /* @__PURE__ */ React.createElement(
      "textarea",
      {
        className: "nt-input nt-textarea",
        rows: 3,
        value: note,
        onChange: (event) => setNote(event.target.value)
      }
    )), /* @__PURE__ */ React.createElement("div", { className: "nt-panel-foot" }, /* @__PURE__ */ React.createElement("button", { type: "button", className: "nt-btn nt-btn-ghost", onClick: onCancel, disabled: saving }, "\u53D6\u6D88"), /* @__PURE__ */ React.createElement(
      "button",
      {
        type: "button",
        className: "nt-btn",
        disabled: saving,
        onClick: () => onSave({
          tier,
          skills,
          affix_main: main,
          affix_sub: sub,
          affix_note: affixNote,
          note
        })
      },
      saving ? "\u4FDD\u5B58\u4E2D\u2026" : "\u4FDD\u5B58\u6821\u6838"
    )));
  }
  function TargetSummary({ target, onEdit, inline }) {
    const requirement = target && target.requirement || {};
    const main = requirement.affixes || [];
    const sub = requirement.sub_affixes || [];
    const mainText = requirement.no_requirement ? "\u65E0\u7279\u6B8A\u8981\u6C42" : main.length ? main.map((item) => item.text || item.affix).join(" / ") : requirement.known ? "\u2014" : "\u672A\u6821\u6838";
    const subText = sub.length ? sub.map((item) => item.text || item.affix).join(" / ") : "\u2014";
    return /* @__PURE__ */ React.createElement("div", { className: "nt-target-box" + (inline ? " is-inline" : "") }, /* @__PURE__ */ React.createElement("div", { className: "nt-target-head" }, /* @__PURE__ */ React.createElement("span", { className: "nt-target-title" }, "\u76EE\u6807\u57FA\u51C6"), target && target.guide_date ? /* @__PURE__ */ React.createElement("span", { className: "nt-muted" }, "\u4E00\u56FE\u6D41 ", target.guide_date) : null, /* @__PURE__ */ React.createElement("button", { type: "button", className: "nt-btn nt-btn-ghost", onClick: onEdit }, "\u6821\u6838")), /* @__PURE__ */ React.createElement("div", { className: "nt-target-row" }, /* @__PURE__ */ React.createElement("span", null, "\u6838\u5FC3\u8BCD\u6761"), /* @__PURE__ */ React.createElement("strong", { className: requirement.no_requirement ? "nt-req-none" : "" }, mainText)), /* @__PURE__ */ React.createElement("div", { className: "nt-target-row" }, /* @__PURE__ */ React.createElement("span", null, "\u526F\u8BCD\u6761"), /* @__PURE__ */ React.createElement("strong", null, subText)), /* @__PURE__ */ React.createElement("div", { className: "nt-target-row" }, /* @__PURE__ */ React.createElement("span", null, "\u9B54\u65B9 / \u6536\u85CF\u54C1"), /* @__PURE__ */ React.createElement("strong", null, target && target.cube || "\u2014", " / ", target && target.collectible || "\u2014")), target && target.affix_note ? /* @__PURE__ */ React.createElement("div", { className: "nt-target-note", title: target.affix_note }, target.affix_note) : null);
  }
  function DetailModal({ title, onClose, actions, children }) {
    useEffect(() => {
      const onKey = (event) => {
        if (event.key === "Escape") onClose();
      };
      document.addEventListener("keydown", onKey);
      const previous = document.body.style.overflow;
      document.body.style.overflow = "hidden";
      return () => {
        document.removeEventListener("keydown", onKey);
        document.body.style.overflow = previous;
      };
    }, [onClose]);
    return /* @__PURE__ */ React.createElement(
      "div",
      {
        className: "nt-modal-overlay",
        onMouseDown: (event) => {
          if (event.target === event.currentTarget) onClose();
        }
      },
      /* @__PURE__ */ React.createElement("div", { className: "nt-modal", role: "dialog", "aria-modal": "true", "aria-label": title || "\u89D2\u8272\u8BE6\u60C5" }, /* @__PURE__ */ React.createElement("div", { className: "nt-modal-bar" }, /* @__PURE__ */ React.createElement("div", { className: "nt-modal-title" }, title || "\u89D2\u8272\u8BE6\u60C5"), /* @__PURE__ */ React.createElement("div", { className: "nt-modal-actions" }, actions, /* @__PURE__ */ React.createElement("button", { type: "button", className: "nt-modal-close", onClick: onClose, "aria-label": "\u5173\u95ED" }, "\xD7"))), /* @__PURE__ */ React.createElement("div", { className: "nt-modal-body" }, children))
    );
  }
  function Detail({ name, bootstrap, onClose, onSaved, onAdd, onFetchPortrait, onTargetSaved, onPortraitUploaded, onToggleFavorite, toast }) {
    const initial = cachedDetail(name, bootstrap);
    const [detail, setDetail] = useState(initial);
    const [loading, setLoading] = useState(!initial);
    const [error, setError] = useState(null);
    const [conflict, setConflict] = useState(false);
    const [removing, setRemoving] = useState(false);
    const [adding, setAdding] = useState(false);
    const [fetchingPortrait, setFetchingPortrait] = useState(false);
    const [editingTarget, setEditingTarget] = useState(false);
    const [savingTarget, setSavingTarget] = useState(false);
    const [uploadingPortrait, setUploadingPortrait] = useState(false);
    const fileInputRef = useRef(null);
    const load = () => {
      setLoading(true);
      setError(null);
      getDetail(name).then((next) => {
        setDetail(next);
        setConflict(false);
      }).catch((err) => setError(err)).finally(() => setLoading(false));
    };
    useEffect(() => {
      const hit = cachedDetail(name, bootstrap);
      if (hit) {
        setDetail(hit);
        setLoading(false);
        setError(null);
        setConflict(false);
        return;
      }
      load();
    }, [name]);
    useEffect(() => {
      if (detail) storeDetail(detail);
    }, [detail]);
    useEffect(() => {
      if (!editingTarget) return;
      const node = document.getElementById("nt-target-editor");
      if (node && node.scrollIntoView) node.scrollIntoView({ block: "nearest" });
    }, [editingTarget]);
    const wallItem = useMemo(() => {
      const list = bootstrap && bootstrap.characters || [];
      const canonical = detail && detail.character && detail.character.name || name;
      return list.find((item) => item.name === canonical) || null;
    }, [bootstrap, name, detail]);
    if (loading && !detail) {
      return /* @__PURE__ */ React.createElement(DetailModal, { title: name, onClose }, /* @__PURE__ */ React.createElement("div", { className: "nt-detail-skeleton" }, /* @__PURE__ */ React.createElement("div", { className: "nt-skeleton-art" }), /* @__PURE__ */ React.createElement("div", { className: "nt-skeleton-line" }), /* @__PURE__ */ React.createElement("div", { className: "nt-skeleton-line short" })));
    }
    if (error && !detail) {
      return /* @__PURE__ */ React.createElement(DetailModal, { title: name, onClose }, /* @__PURE__ */ React.createElement("div", { className: "nt-error-card" }, /* @__PURE__ */ React.createElement("div", { className: "nt-error-title" }, "\u8BE6\u60C5\u52A0\u8F7D\u5931\u8D25"), /* @__PURE__ */ React.createElement("pre", { className: "nt-error-detail" }, String(error.message || error)), /* @__PURE__ */ React.createElement("button", { type: "button", className: "nt-btn", onClick: load }, "\u91CD\u8BD5")));
    }
    if (!detail) return null;
    const character = detail.character || {};
    const target = detail.target || {};
    const diag = detail.diag;
    const owned = Boolean(detail.owned);
    const isFavorite = Boolean(detail.favorite);
    const canEdit = Boolean(detail.rev);
    const portrait = wallItem && wallItem.portrait;
    const handleToggleFavorite = async () => {
      const previous = isFavorite;
      const next = !isFavorite;
      setDetail((state) => ({ ...state, favorite: next }));
      const ok = await onToggleFavorite(character.name, next);
      if (ok === false) {
        setDetail((state) => ({ ...state, favorite: previous }));
      }
    };
    const handleSaveGear = async (pieces) => {
      try {
        const payload = await saveGearBatch({ name: character.name, pieces, rev: detail.rev });
        setDetail((previous) => ({
          ...previous,
          rev: payload.rev,
          character: payload.character,
          target: payload.target,
          diag: payload.diag,
          owned: payload.owned,
          favorite: payload.favorite
        }));
        if (onSaved) onSaved(payload);
        const render = payload.render;
        if (render && render.ok) {
          toast(render.changed ? "\u5DF2\u4FDD\u5B58\u5E76\u540C\u6B65 nikke.md" : "\u5DF2\u4FDD\u5B58\uFF08Markdown \u65E0\u53D8\u5316\uFF09", "ok");
        } else {
          toast("\u5DF2\u4FDD\u5B58\uFF0C\u4F46\u6E32\u67D3\u5931\u8D25\uFF1A" + (render && render.error || "\u672A\u77E5\u9519\u8BEF"), "warn");
        }
      } catch (err) {
        if (err.status === 409) {
          setConflict(true);
          toast("\u6570\u636E\u5DF2\u88AB\u5176\u4ED6\u9875\u9762\u66F4\u65B0\uFF0C\u8BF7\u5148\u5237\u65B0\u8BE6\u60C5", "warn");
        } else {
          toast(err.message || "\u4FDD\u5B58\u5931\u8D25", "error");
        }
        throw err;
      }
    };
    const handleSaveMeta = async (patch) => {
      try {
        const payload = await saveMeta({ name: character.name, ...patch, rev: detail.rev });
        setDetail((previous) => ({
          ...previous,
          rev: payload.rev,
          character: payload.character,
          target: payload.target,
          diag: payload.diag,
          owned: payload.owned,
          favorite: payload.favorite
        }));
        if (onSaved) onSaved(payload);
        const render = payload.render;
        toast(render && render.ok ? "\u5DF2\u4FDD\u5B58\u5E76\u540C\u6B65 nikke.md" : "\u5DF2\u4FDD\u5B58\uFF0C\u4F46\u6E32\u67D3\u5931\u8D25", render && render.ok ? "ok" : "warn");
      } catch (err) {
        if (err.status === 409) {
          setConflict(true);
          toast("\u6570\u636E\u5DF2\u88AB\u5176\u4ED6\u9875\u9762\u66F4\u65B0\uFF0C\u8BF7\u5148\u5237\u65B0\u8BE6\u60C5", "warn");
        } else {
          toast(err.message || "\u4FDD\u5B58\u5931\u8D25", "error");
        }
        throw err;
      }
    };
    const handleRemove = async () => {
      if (!window.confirm("\u786E\u5B9A\u628A\u300C" + character.name + "\u300D\u79FB\u51FA\u770B\u677F\uFF1F\u8FDB\u5EA6\u6570\u636E\u4F1A\u88AB\u5220\u9664\u3002")) return;
      setRemoving(true);
      try {
        await removeCharacter(character.name, detail.rev);
        toast("\u5DF2\u79FB\u51FA\u770B\u677F", "ok");
        onClose(true);
      } catch (err) {
        toast(err.message || "\u79FB\u51FA\u5931\u8D25", "error");
      } finally {
        setRemoving(false);
      }
    };
    const handleAdd = async () => {
      setAdding(true);
      try {
        await onAdd(character.name);
        load();
      } catch (err) {
        toast(err.message || "\u52A0\u5165\u5931\u8D25", "error");
      } finally {
        setAdding(false);
      }
    };
    const handleFetchPortrait = async () => {
      setFetchingPortrait(true);
      try {
        await onFetchPortrait(character.name, true);
      } finally {
        setFetchingPortrait(false);
      }
    };
    const handleSaveTarget = async (patch) => {
      setSavingTarget(true);
      try {
        const payload = await saveTarget({
          name: character.name,
          ...patch,
          targets_rev: detail.targets_rev
        });
        setDetail((previous) => ({
          ...previous,
          rev: payload.rev,
          targets_rev: payload.targets_rev,
          character: payload.character,
          target: payload.target,
          diag: payload.diag,
          owned: payload.owned,
          favorite: payload.favorite
        }));
        setEditingTarget(false);
        if (onTargetSaved) onTargetSaved(payload);
        const render = payload.render;
        toast(
          render && render.ok ? "\u63A8\u8350\u4FE1\u606F\u5DF2\u6821\u6838\u5E76\u540C\u6B65 nikke.md" : "\u63A8\u8350\u4FE1\u606F\u5DF2\u4FDD\u5B58\uFF0C\u4F46 Markdown \u6E32\u67D3\u5931\u8D25",
          render && render.ok ? "ok" : "warn"
        );
      } catch (err) {
        toast(err.message || "\u6821\u6838\u4FDD\u5B58\u5931\u8D25", "error");
      } finally {
        setSavingTarget(false);
      }
    };
    const handlePortraitFile = (event) => {
      const file = event.target.files && event.target.files[0];
      event.target.value = "";
      if (!file) return;
      if (file.size > 8 * 1024 * 1024) {
        toast("\u56FE\u7247\u4E0D\u80FD\u8D85\u8FC7 8MB", "warn");
        return;
      }
      const reader = new FileReader();
      reader.onload = async () => {
        setUploadingPortrait(true);
        try {
          const result = await uploadPortrait(character.name, String(reader.result || ""));
          toast("\u5934\u50CF\u5DF2\u66F4\u65B0", "ok");
          if (onPortraitUploaded) await onPortraitUploaded(result);
        } catch (err) {
          toast(err.message || "\u5934\u50CF\u4E0A\u4F20\u5931\u8D25", "error");
        } finally {
          setUploadingPortrait(false);
        }
      };
      reader.readAsDataURL(file);
    };
    return /* @__PURE__ */ React.createElement(
      DetailModal,
      {
        title: character.name,
        onClose,
        actions: /* @__PURE__ */ React.createElement(React.Fragment, null, /* @__PURE__ */ React.createElement(
          "button",
          {
            type: "button",
            className: "nt-btn nt-btn-ghost" + (isFavorite ? " is-fav" : ""),
            onClick: handleToggleFavorite
          },
          isFavorite ? "\u2605 \u5DF2\u6536\u85CF" : "\u2606 \u52A0\u5165\u961F\u5217"
        ), owned ? /* @__PURE__ */ React.createElement("button", { type: "button", className: "nt-btn nt-btn-danger", onClick: handleRemove, disabled: removing }, removing ? "\u79FB\u51FA\u4E2D\u2026" : "\u79FB\u51FA\u770B\u677F") : null, !portrait || !portrait.has_cache ? /* @__PURE__ */ React.createElement("button", { type: "button", className: "nt-btn nt-btn-ghost", onClick: handleFetchPortrait, disabled: fetchingPortrait }, fetchingPortrait ? "\u6293\u53D6\u4E2D\u2026" : "\u8865\u5934\u50CF") : null, /* @__PURE__ */ React.createElement(
          "button",
          {
            type: "button",
            className: "nt-btn nt-btn-ghost",
            onClick: () => fileInputRef.current && fileInputRef.current.click(),
            disabled: uploadingPortrait
          },
          uploadingPortrait ? "\u4E0A\u4F20\u4E2D\u2026" : "\u4E0A\u4F20\u5934\u50CF"
        ), /* @__PURE__ */ React.createElement(
          "input",
          {
            ref: fileInputRef,
            type: "file",
            accept: "image/png,image/jpeg,image/webp",
            className: "nt-file-hidden",
            onChange: handlePortraitFile
          }
        ))
      },
      /* @__PURE__ */ React.createElement("section", { className: "nt-hero" }, /* @__PURE__ */ React.createElement("div", { className: "nt-hero-art" }, /* @__PURE__ */ React.createElement(Portrait, { name: character.name, element: character.element, portrait, className: "nt-hero-portrait" })), /* @__PURE__ */ React.createElement("div", { className: "nt-hero-info" }, /* @__PURE__ */ React.createElement("div", { className: "nt-hero-title" }, /* @__PURE__ */ React.createElement(TierBadge2, { tier: character.tier }), /* @__PURE__ */ React.createElement("h2", null, character.name), /* @__PURE__ */ React.createElement("span", { className: "nt-muted" }, character.element, " / ", character.class, " \xB7 \u7206\u88C2 ", character.burst, " \xB7 ", character.weapon)), /* @__PURE__ */ React.createElement("div", { className: "nt-role" }, character.role || "\u2014"), owned ? /* @__PURE__ */ React.createElement("div", { className: "nt-hero-stats" }, /* @__PURE__ */ React.createElement("div", { className: "nt-hero-score" }, /* @__PURE__ */ React.createElement(BigRing, { score: diag ? diag.score : 0, grade: diag ? diag.grade : "" })), /* @__PURE__ */ React.createElement("div", { className: "nt-hero-facts" }, /* @__PURE__ */ React.createElement("div", null, /* @__PURE__ */ React.createElement("span", null, "\u6280\u80FD"), /* @__PURE__ */ React.createElement("strong", null, (character.skills || []).join("/")), /* @__PURE__ */ React.createElement("em", null, "\u76EE\u6807 ", (target.skills || []).join("/"))), /* @__PURE__ */ React.createElement("div", null, /* @__PURE__ */ React.createElement("span", null, "\u6709\u6548\u8BCD\u6761"), /* @__PURE__ */ React.createElement("strong", null, diag ? diag.effective : 0, " \u6761"), /* @__PURE__ */ React.createElement("em", null, "\u6309\u5F53\u524D\u88C5\u5907\u7EDF\u8BA1")), /* @__PURE__ */ React.createElement("div", null, /* @__PURE__ */ React.createElement("span", null, "\u9B54\u65B9"), /* @__PURE__ */ React.createElement("strong", null, target.cube || "\u2014"), /* @__PURE__ */ React.createElement("em", null, "\u6536\u85CF\u54C1 ", character.collectible || "\u2014")), /* @__PURE__ */ React.createElement("div", null, /* @__PURE__ */ React.createElement("span", null, "\u4E0B\u4E00\u52A8\u4F5C"), /* @__PURE__ */ React.createElement("strong", { className: "nt-priority" }, diag ? diag.priority : "\u2014")))) : /* @__PURE__ */ React.createElement("div", { className: "nt-add-box" }, /* @__PURE__ */ React.createElement("p", null, "\u8FD9\u4E2A\u89D2\u8272\u8FD8\u6CA1\u6709\u52A0\u5165\u770B\u677F\u3002\u70B9\u5361\u7247\u65F6\u4F1A\u81EA\u52A8\u52A0\u5165\uFF1B\u76F4\u63A5\u6253\u5F00\u8BE6\u60C5\u65F6\u4E5F\u53EF\u4EE5\u5728\u8FD9\u91CC\u52A0\u5165\u3002\u52A0\u5165\u540E\u4EE5\u6280\u80FD 1/1/1\u3001\u7A7A\u88C5\u5907\u5F00\u59CB\uFF0C\u4E0D\u4F1A\u81EA\u52A8\u586B\u76EE\u6807\u7B49\u7EA7\u3002"), /* @__PURE__ */ React.createElement("button", { type: "button", className: "nt-btn", onClick: handleAdd, disabled: adding }, adding ? "\u52A0\u5165\u4E2D\u2026" : "\u52A0\u5165\u770B\u677F")), /* @__PURE__ */ React.createElement(TargetSummary, { target, inline: true, onEdit: () => setEditingTarget(true) }))),
      conflict ? /* @__PURE__ */ React.createElement("div", { className: "nt-conflict" }, /* @__PURE__ */ React.createElement("span", null, "\u6570\u636E\u5DF2\u88AB\u5176\u4ED6\u9875\u9762\u66F4\u65B0\u3002\u5237\u65B0\u4F1A\u653E\u5F03\u4F60\u5F53\u524D\u672A\u4FDD\u5B58\u7684\u4FEE\u6539\u3002"), /* @__PURE__ */ React.createElement("button", { type: "button", className: "nt-btn nt-btn-ghost", onClick: load }, "\u5237\u65B0\u8BE6\u60C5")) : null,
      editingTarget ? /* @__PURE__ */ React.createElement("div", { id: "nt-target-editor" }, /* @__PURE__ */ React.createElement(
        TargetEditor,
        {
          target,
          affixOptions: detail.affix_options || bootstrap && bootstrap.affix_options || [],
          onSave: handleSaveTarget,
          onCancel: () => setEditingTarget(false),
          saving: savingTarget
        }
      )) : null,
      owned ? /* @__PURE__ */ React.createElement("div", { className: "nt-detail-grid" }, /* @__PURE__ */ React.createElement(
        GearBoard,
        {
          key: character.name,
          gear: character.gear,
          diag,
          target,
          affixMeta: detail.affix_meta || bootstrap && bootstrap.affix_meta || {},
          affixOptions: detail.affix_options || bootstrap && bootstrap.affix_options || [],
          serverRev: detail.rev,
          onSave: handleSaveGear,
          disabled: Boolean(conflict) || !canEdit
        }
      ), /* @__PURE__ */ React.createElement("div", { className: "nt-side" }, /* @__PURE__ */ React.createElement(AdvicePanel, { diag }), /* @__PURE__ */ React.createElement(MetaEditor, { character, target, onSave: handleSaveMeta, disabled: Boolean(conflict) || !canEdit }))) : null
    );
  }

  // dashboard/src/teams.jsx
  function Block({ block }) {
    if (block.type === "heading") {
      const Tag = "h" + Math.min(6, Math.max(2, block.level || 2));
      return /* @__PURE__ */ React.createElement(Tag, { className: "nt-md-h nt-md-h" + (block.level || 2) }, block.text);
    }
    if (block.type === "paragraph") {
      return /* @__PURE__ */ React.createElement("p", { className: "nt-md-p" }, block.text);
    }
    if (block.type === "list") {
      return /* @__PURE__ */ React.createElement("ul", { className: "nt-md-list" }, (block.items || []).map((item, index) => /* @__PURE__ */ React.createElement("li", { key: index }, item)));
    }
    if (block.type === "table") {
      return /* @__PURE__ */ React.createElement("div", { className: "nt-md-table-wrap" }, /* @__PURE__ */ React.createElement("table", { className: "nt-md-table" }, /* @__PURE__ */ React.createElement("thead", null, /* @__PURE__ */ React.createElement("tr", null, (block.headers || []).map((cell, index) => /* @__PURE__ */ React.createElement("th", { key: index }, cell)))), /* @__PURE__ */ React.createElement("tbody", null, (block.rows || []).map((row, rowIndex) => /* @__PURE__ */ React.createElement("tr", { key: rowIndex }, row.map((cell, cellIndex) => /* @__PURE__ */ React.createElement("td", { key: cellIndex }, cell)))))));
    }
    return null;
  }
  function TeamDoc({ doc, tone }) {
    if (!doc) return null;
    return /* @__PURE__ */ React.createElement("article", { className: "nt-team-doc nt-team-" + tone }, tone === "solo" ? /* @__PURE__ */ React.createElement("div", { className: "nt-team-banner" }, "\u5355\u4EBA\u7A81\u51FB\u6BCF\u8D5B\u5B63\u6362 Boss\uFF0C\u5C5E\u6027\u548C\u5F31\u70B9\u5FC5\u987B\u6309\u5F53\u5B63\u516C\u544A\u6838\u5BF9\uFF1B\u672C\u9875\u662F\u5F53\u524D\u6280\u80FD\u91CC\u7684\u8D5B\u5B63\u8BB0\u5F55\uFF0C\u4E0D\u4FDD\u8BC1\u4ECD\u7136\u6709\u6548\u3002") : null, (doc.blocks || []).length ? (doc.blocks || []).map((block, index) => /* @__PURE__ */ React.createElement(Block, { key: index, block })) : /* @__PURE__ */ React.createElement("pre", { className: "nt-raw" }, doc.raw || "\u6682\u65E0\u5185\u5BB9"), /* @__PURE__ */ React.createElement("footer", { className: "nt-team-foot" }, "\u6765\u6E90 ", /* @__PURE__ */ React.createElement("span", { className: "nt-mono" }, doc.source), doc.updated ? /* @__PURE__ */ React.createElement("span", null, " \xB7 \u6587\u4EF6\u66F4\u65B0 ", doc.updated.slice(0, 16).replace("T", " ")) : null));
  }
  function Teams({ data, loading, error, onRetry }) {
    const [tab, setTab] = useState("interception");
    if (loading && !data) {
      return /* @__PURE__ */ React.createElement("div", { className: "nt-content" }, /* @__PURE__ */ React.createElement("div", { className: "nt-detail-skeleton" }, /* @__PURE__ */ React.createElement("div", { className: "nt-skeleton-line" }), /* @__PURE__ */ React.createElement("div", { className: "nt-skeleton-line short" }), /* @__PURE__ */ React.createElement("div", { className: "nt-skeleton-line" })));
    }
    if (error && !data) {
      return /* @__PURE__ */ React.createElement("div", { className: "nt-content" }, /* @__PURE__ */ React.createElement("div", { className: "nt-error-card" }, /* @__PURE__ */ React.createElement("div", { className: "nt-error-title" }, "\u914D\u961F\u53C2\u8003\u52A0\u8F7D\u5931\u8D25"), /* @__PURE__ */ React.createElement("pre", { className: "nt-error-detail" }, String(error.message || error)), /* @__PURE__ */ React.createElement("button", { type: "button", className: "nt-btn", onClick: onRetry }, "\u91CD\u8BD5")));
    }
    return /* @__PURE__ */ React.createElement("div", { className: "nt-content nt-teams" }, /* @__PURE__ */ React.createElement("div", { className: "nt-team-tabs", role: "tablist" }, /* @__PURE__ */ React.createElement(
      "button",
      {
        type: "button",
        role: "tab",
        "aria-selected": tab === "interception",
        className: "nt-team-tab" + (tab === "interception" ? " is-active" : ""),
        onClick: () => setTab("interception")
      },
      "\u5F02\u5E38\u62E6\u622A"
    ), /* @__PURE__ */ React.createElement(
      "button",
      {
        type: "button",
        role: "tab",
        "aria-selected": tab === "solo",
        className: "nt-team-tab" + (tab === "solo" ? " is-active" : ""),
        onClick: () => setTab("solo")
      },
      "\u5355\u4EBA\u7A81\u51FB"
    )), tab === "interception" ? /* @__PURE__ */ React.createElement(TeamDoc, { doc: data && data.interception, tone: "interception" }) : /* @__PURE__ */ React.createElement(TeamDoc, { doc: data && data.solo_raid, tone: "solo" }));
  }

  // dashboard/src/index.jsx
  function App() {
    const [data, setData] = useState(null);
    const [loading, setLoading] = useState(true);
    const [error, setError] = useState(null);
    const [view, setView] = useState("wall");
    const [detailName, setDetailName] = useState(null);
    const [openingName, setOpeningName] = useState(null);
    const [teams, setTeams] = useState(null);
    const [teamsLoading, setTeamsLoading] = useState(false);
    const [teamsError, setTeamsError] = useState(null);
    const [toasts, setToasts] = useState([]);
    const [rendering, setRendering] = useState(false);
    const toast = useCallback((message, tone) => {
      const id = Date.now() + Math.random();
      setToasts((previous) => [...previous, { id, message, tone: tone || "ok" }]);
      window.setTimeout(() => {
        setToasts((previous) => previous.filter((item) => item.id !== id));
      }, 3600);
    }, []);
    const loadBootstrap = useCallback(async () => {
      setLoading(true);
      setError(null);
      try {
        const next = await getBootstrap();
        setData(next);
      } catch (err) {
        setError(err);
      } finally {
        setLoading(false);
      }
    }, []);
    const patchCharacter = useCallback((name, patch) => {
      setData((previous) => {
        if (!previous) return previous;
        return {
          ...previous,
          characters: previous.characters.map(
            (character) => character.name === name ? { ...character, ...patch } : character
          )
        };
      });
    }, []);
    const applyPayload = useCallback((payload) => {
      setData((previous) => {
        if (!previous) return previous;
        const summary = summaryFromPayload(payload);
        const nextFavorites = payload.favorite ? Array.from(/* @__PURE__ */ new Set([...previous.favorites || [], payload.character.name])) : (previous.favorites || []).filter((item) => item !== payload.character.name);
        return {
          ...previous,
          rev: payload.rev,
          targets_rev: payload.targets_rev || previous.targets_rev,
          favorites: nextFavorites,
          counts: {
            ...previous.counts,
            favorites: nextFavorites.length,
            owned: payload.created ? (previous.counts.owned || 0) + 1 : previous.counts.owned
          },
          characters: previous.characters.map(
            (character) => character.name === payload.character.name ? { ...character, ...summary, owned: true } : character
          )
        };
      });
    }, []);
    const refreshPortrait = useCallback(async (name, notify) => {
      try {
        const result = await fetchPortrait(name);
        if (result && result.fetched && result.portrait) {
          if (notify) toast("\u5934\u50CF\u5DF2\u8865\u4E0A", "ok");
          patchCharacter(result.name || name, { portrait: result.portrait });
        }
        return result;
      } catch (err) {
        if (notify) toast(err.message || "\u5934\u50CF\u6293\u53D6\u5931\u8D25", "warn");
        return null;
      }
    }, [patchCharacter, toast]);
    useEffect(() => {
      loadBootstrap();
    }, [loadBootstrap]);
    const openDetail = async (name) => {
      const character = data && data.characters.find((item) => item.name === name);
      if (character && !character.owned) {
        setOpeningName(name);
        try {
          const payload = await addCharacter(name);
          toast("\u5DF2\u81EA\u52A8\u52A0\u5165\u770B\u677F", "ok");
          storeDetail(payload);
          applyPayload(payload);
          setDetailName(payload.character.name);
          refreshPortrait(payload.character.name, false);
        } catch (err) {
          toast(err.message || "\u81EA\u52A8\u52A0\u5165\u5931\u8D25", "error");
          setDetailName(name);
        } finally {
          setOpeningName(null);
        }
        return;
      }
      setDetailName(name);
      if (character && character.owned && (!character.portrait || !character.portrait.has_cache)) {
        refreshPortrait(name, false);
      }
    };
    const closeDetail = (refresh) => {
      setDetailName(null);
      if (refresh) loadBootstrap();
    };
    const toggleFavorite = async (name, next) => {
      if (!data) return;
      const current = data.favorites || [];
      const nextFavorites = next ? Array.from(/* @__PURE__ */ new Set([...current, name])) : current.filter((item) => item !== name);
      setData((previous) => ({
        ...previous,
        favorites: nextFavorites,
        characters: previous.characters.map(
          (character) => character.name === name ? { ...character, favorite: next } : character
        )
      }));
      try {
        await setFavorites(nextFavorites);
        return true;
      } catch (err) {
        setData((previous) => ({
          ...previous,
          favorites: current,
          characters: previous.characters.map(
            (character) => character.name === name ? { ...character, favorite: !next } : character
          )
        }));
        toast(err.message || "\u6536\u85CF\u5931\u8D25", "error");
        return false;
      }
    };
    const handleTargetSaved = async () => {
      await loadBootstrap();
    };
    const handlePortraitUploaded = (result) => {
      if (result && result.portrait) {
        patchCharacter(result.name || detailName, { portrait: result.portrait });
      }
    };
    const handleSaved = (payload) => {
      applyPayload(payload);
    };
    const handleAdd = async (name) => {
      const payload = await addCharacter(name);
      toast("\u5DF2\u52A0\u5165\u770B\u677F", "ok");
      storeDetail(payload);
      applyPayload(payload);
      setDetailName(payload.character.name);
      refreshPortrait(payload.character.name, false);
      return payload;
    };
    const handleRender = async () => {
      setRendering(true);
      try {
        const result = await renderNote();
        toast(
          result.ok ? result.changed ? "\u5DF2\u540C\u6B65 nikke.md" : "Markdown \u65E0\u53D8\u5316" : "\u6E32\u67D3\u5931\u8D25\uFF1A" + (result.error || ""),
          result.ok ? "ok" : "warn"
        );
        if (result.ok) loadBootstrap();
      } catch (err) {
        toast(err.message || "\u6E32\u67D3\u5931\u8D25", "error");
      } finally {
        setRendering(false);
      }
    };
    const showTeams = async () => {
      setDetailName(null);
      setView("teams");
      if (teams || teamsLoading) return;
      setTeamsLoading(true);
      setTeamsError(null);
      try {
        const next = await getTeams();
        setTeams(next);
      } catch (err) {
        setTeamsError(err);
      } finally {
        setTeamsLoading(false);
      }
    };
    const showWall = () => {
      setDetailName(null);
      setView("wall");
    };
    const isWall = view === "wall";
    const isTeams = view === "teams";
    return /* @__PURE__ */ React.createElement("div", { className: "nt-root" }, /* @__PURE__ */ React.createElement("header", { className: "nt-topbar" }, /* @__PURE__ */ React.createElement("div", { className: "nt-brand" }, /* @__PURE__ */ React.createElement("h1", null, "NIKKE \u517B\u6210"), /* @__PURE__ */ React.createElement("p", null, "\u88C5\u5907\u8BCD\u6761 \xB7 \u6D17\u6761\u5EFA\u8BAE \xB7 \u914D\u961F\u901F\u67E5")), /* @__PURE__ */ React.createElement("nav", { className: "nt-nav" }, /* @__PURE__ */ React.createElement(
      "button",
      {
        type: "button",
        className: "nt-nav-btn" + (isWall ? " is-active" : ""),
        onClick: showWall
      },
      "\u89D2\u8272\u5899"
    ), /* @__PURE__ */ React.createElement(
      "button",
      {
        type: "button",
        className: "nt-nav-btn" + (isTeams ? " is-active" : ""),
        onClick: showTeams
      },
      "\u914D\u961F\u901F\u67E5"
    ), /* @__PURE__ */ React.createElement("button", { type: "button", className: "nt-nav-btn", onClick: loadBootstrap, disabled: loading }, loading ? "\u5237\u65B0\u4E2D\u2026" : "\u5237\u65B0"))), isWall ? /* @__PURE__ */ React.createElement(
      Wall,
      {
        data,
        loading,
        error,
        onRetry: loadBootstrap,
        onOpen: openDetail,
        openingName,
        onToggleFavorite: toggleFavorite,
        onRender: handleRender,
        rendering
      }
    ) : null, isTeams ? /* @__PURE__ */ React.createElement(Teams, { data: teams, loading: teamsLoading, error: teamsError, onRetry: showTeams }) : null, detailName ? /* @__PURE__ */ React.createElement(
      Detail,
      {
        key: detailName,
        name: detailName,
        bootstrap: data,
        onClose: closeDetail,
        onSaved: handleSaved,
        onAdd: handleAdd,
        onFetchPortrait: refreshPortrait,
        onTargetSaved: handleTargetSaved,
        onPortraitUploaded: handlePortraitUploaded,
        onToggleFavorite: toggleFavorite,
        toast
      }
    ) : null, /* @__PURE__ */ React.createElement("div", { className: "nt-toasts", "aria-live": "polite" }, toasts.map((item) => /* @__PURE__ */ React.createElement("div", { key: item.id, className: "nt-toast nt-toast-" + item.tone }, item.message))));
  }
  window.__HERMES_PLUGINS__.register("nikke-tracker", App);
})();
