/* airss「RSS 展板」仪表盘 UI 插件（无构建、纯 IIFE）。
 * 与 Muse RSS 展板同款结构：置顶页签 展板 / 订阅源 / 总结。
 * 数据经本插件后端 /api/plugins/airss/board/* 读 airss 本地库；
 * 已读/收藏/反馈只写后端 board_state 表；总结开关、分组提示词与排期
 * 写订阅/分组的配置字段，绝不碰 articles.is_sent / article_sent 推送语义。
 * 关键约定：点喜欢/不感兴趣/收藏/标已读只就地更新本地 state，
 * 绝不重新拉整个列表（整表刷新会丢失滚动位置）。
 * 列表为瀑布流：流末尾观察器自动加载下一页；停在底部约 2.5 秒后，
 * 把当前真正在视口里的未读条目标已读（滚到底自动已读）。
 */
(function () {
  "use strict";

  var SDK = window.__HERMES_PLUGIN_SDK__;
  var registry = window.__HERMES_PLUGINS__;
  if (!SDK || !SDK.React || !registry || typeof registry.register !== "function") {
    console.warn("[airss] Hermes plugin SDK 未就绪，跳过注册");
    return;
  }

  var React = SDK.React;
  var useState = SDK.hooks.useState;
  var useEffect = SDK.hooks.useEffect;
  var useCallback = SDK.hooks.useCallback || function (f) { return f; };
  var el = React.createElement;

  var API = "/api/plugins/airss";
  var PAGE_SIZE = 50;
  var BOTTOM_DWELL_MS = 2500;

  function fetchJSON(path) {
    if (SDK.fetchJSON) return SDK.fetchJSON(path);
    return fetch(path).then(function (r) {
      if (!r.ok) throw new Error("HTTP " + r.status);
      return r.json();
    });
  }

  function postJSON(path, body) {
    return fetch(path, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body || {}),
    }).then(function (r) {
      if (!r.ok) throw new Error("HTTP " + r.status);
      return r.json();
    });
  }

  function fmtTime(item) {
    var t = item.published_at || item.fetched_at || "";
    return String(t).replace("T", " ").slice(0, 16);
  }

  function copyText(text) {
    if (navigator.clipboard && navigator.clipboard.writeText) {
      return navigator.clipboard.writeText(text);
    }
    return Promise.reject(new Error("clipboard unavailable"));
  }

  var S = {
    page: { padding: 16, maxWidth: 1440, margin: "0 auto" },
    tabs: {
      position: "sticky", top: 0, zIndex: 50, display: "flex", gap: 8,
      padding: "10px 0", marginBottom: 10,
      background: "var(--background, #1c1c1e)",
      borderBottom: "1px solid rgba(127,127,127,.25)",
    },
    tab: {
      padding: "6px 18px", borderRadius: 999, border: "1px solid rgba(127,127,127,.4)",
      background: "transparent", cursor: "pointer", fontSize: 14,
    },
    tabOn: { background: "rgba(127,127,127,.30)", fontWeight: 700 },
    header: { display: "flex", alignItems: "center", gap: 10, flexWrap: "wrap", marginBottom: 10 },
    title: { fontSize: 20, fontWeight: 700 },
    unreadBadge: {
      background: "#d33", color: "#fff", borderRadius: 999, padding: "1px 9px",
      fontSize: 12, fontWeight: 600,
    },
    toolbar: { display: "flex", alignItems: "center", gap: 8, flexWrap: "wrap", marginBottom: 10 },
    chip: {
      padding: "4px 12px", borderRadius: 999, border: "1px solid rgba(127,127,127,.4)",
      background: "transparent", cursor: "pointer", fontSize: 13,
    },
    chipOn: { background: "rgba(127,127,127,.28)", fontWeight: 700 },
    btn: {
      padding: "5px 12px", borderRadius: 8, border: "1px solid rgba(127,127,127,.4)",
      background: "transparent", cursor: "pointer", fontSize: 13,
    },
    grid: {
      display: "grid", gap: 14,
      gridTemplateColumns: "repeat(auto-fill, minmax(270px, 1fr))",
    },
    listWrap: { display: "flex", flexDirection: "column", gap: 8 },
    row: {
      border: "1px solid rgba(127,127,127,.3)", borderRadius: 10, padding: "10px 12px",
      cursor: "pointer", display: "flex", gap: 12, alignItems: "flex-start",
    },
    card: {
      border: "1px solid rgba(127,127,127,.3)", borderRadius: 10, overflow: "hidden",
      cursor: "pointer", display: "flex", flexDirection: "column",
    },
    cardBody: { padding: "10px 12px", display: "flex", flexDirection: "column", gap: 6, flex: 1 },
    cardTitle: { fontSize: 15, fontWeight: 600, lineHeight: 1.4 },
    metaLine: { fontSize: 12, opacity: 0.65, display: "flex", gap: 8, flexWrap: "wrap" },
    fbRow: { display: "flex", gap: 8, marginTop: "auto", paddingTop: 4, flexWrap: "wrap" },
    fbBtn: {
      padding: "3px 10px", borderRadius: 999, fontSize: 12, cursor: "pointer",
      border: "1px solid rgba(127,127,127,.4)", background: "transparent",
    },
    fbOn: { background: "rgba(127,127,127,.30)", fontWeight: 700 },
    aiBadge: {
      fontSize: 11, padding: "1px 7px", borderRadius: 999,
      background: "rgba(200,140,0,.25)", border: "1px solid rgba(200,140,0,.6)",
    },
    srcBadge: {
      fontSize: 11, padding: "1px 7px", borderRadius: 999,
      background: "rgba(80,140,220,.18)", border: "1px solid rgba(80,140,220,.55)",
    },
    overlayBg: {
      position: "fixed", inset: 0, background: "rgba(0,0,0,.55)", zIndex: 999,
      display: "flex", alignItems: "flex-start", justifyContent: "center",
      overflowY: "auto", padding: "24px 12px",
    },
    panel: {
      background: "var(--background, #1c1c1e)", color: "inherit", borderRadius: 12,
      maxWidth: 760, width: "100%", padding: "20px 22px",
      boxShadow: "0 12px 40px rgba(0,0,0,.45)",
    },
    empty: { padding: "48px 16px", textAlign: "center", opacity: 0.75, fontSize: 14 },
    subRow: {
      border: "1px solid rgba(127,127,127,.3)", borderRadius: 10, padding: "10px 12px",
      display: "flex", gap: 10, alignItems: "center", flexWrap: "wrap",
    },
    switchBtn: {
      padding: "3px 10px", borderRadius: 999, fontSize: 12, cursor: "pointer",
      border: "1px solid rgba(127,127,127,.45)", background: "transparent", minWidth: 74,
    },
    switchOn: { background: "rgba(60,160,80,.30)", borderColor: "rgba(60,160,80,.7)", fontWeight: 700 },
    textarea: {
      width: "100%", minHeight: 120, padding: "8px 10px", borderRadius: 8, fontSize: 13,
      lineHeight: 1.6, boxSizing: "border-box",
      background: "transparent", color: "inherit",
      border: "1px solid rgba(127,127,127,.4)",
    },
    input: {
      padding: "5px 9px", borderRadius: 8, fontSize: 13,
      background: "transparent", color: "inherit",
      border: "1px solid rgba(127,127,127,.4)",
    },
    groupCard: {
      border: "1px solid rgba(127,127,127,.3)", borderRadius: 10, padding: "14px 16px",
      marginBottom: 14, display: "flex", flexDirection: "column", gap: 8,
    },
    note: { fontSize: 12, opacity: 0.7, lineHeight: 1.6 },
  };

  function BoardPage() {
    var tabState = useState("board"); // board | subs | summary
    var tab = tabState[0], setTab = tabState[1];

    var availState = useState(null);
    var available = availState[0], setAvailable = availState[1];
    var countsState = useState({ total: 0, unread: 0, starred: 0, filtered: 0 });
    var counts = countsState[0], setCounts = countsState[1];
    var groupsState = useState([]);
    var groups = groupsState[0], setGroups = groupsState[1];
    var subsState = useState([]);
    var subs = subsState[0], setSubs = subsState[1];

    var viewState = useState("unread");
    var view = viewState[0], setView = viewState[1];
    var layoutState = useState("grid");
    var layout = layoutState[0], setLayout = layoutState[1];
    var groupState = useState(null);
    var groupId = groupState[0], setGroupId = groupState[1];
    var subState = useState(null);
    var subId = subState[0], setSubId = subState[1];
    var qInState = useState("");
    var qInput = qInState[0], setQInput = qInState[1];
    var qState = useState("");
    var query = qState[0], setQuery = qState[1];

    var itemsState = useState([]);
    var items = itemsState[0], setItems = itemsState[1];
    var totalState = useState(0);
    var total = totalState[0], setTotal = totalState[1];
    var offState = useState(0);
    var offset = offState[0], setOffset = offState[1];
    var loadState = useState(false);
    var loading = loadState[0], setLoading = loadState[1];
    var errState = useState("");
    var loadErr = errState[0], setLoadErr = errState[1];

    var curState = useState(null);
    var current = curState[0], setCurrent = curState[1];
    var detState = useState(null);
    var detail = detState[0], setDetail = detState[1];

    var sumState = useState(null); // /board/summary 的返回
    var summary = sumState[0], setSummary = sumState[1];
    var sumLoadState = useState(false);
    var sumLoading = sumLoadState[0], setSumLoading = sumLoadState[1];

    // 给观察器用的可变引用盒（避免闭包抓到旧 state）
    var refBox = useState(function () { return { current: {} }; })[0];
    refBox.current.items = items;
    refBox.current.view = view;
    refBox.current.total = total;
    refBox.current.loading = loading;

    function loadMeta() {
      return fetchJSON(API + "/board/meta")
        .then(function (d) {
          setAvailable(!!d.available);
          if (d.available) {
            setCounts(d.counts || { total: 0, unread: 0, starred: 0, filtered: 0 });
            setGroups(d.groups || []);
            setSubs(d.subscriptions || []);
          }
        })
        .catch(function () { setAvailable(false); });
    }

    function loadSummary() {
      setSumLoading(true);
      return fetchJSON(API + "/board/summary")
        .then(function (d) { setSummary(d); if (d) setAvailable(!!d.available); })
        .catch(function () {})
        .then(function () { setSumLoading(false); });
    }

    var loadItems = useCallback(function (reset, off) {
      var useOff = reset ? 0 : (off || 0);
      setLoading(true);
      setLoadErr("");
      var params = [
        "view=" + encodeURIComponent(view),
        "limit=" + PAGE_SIZE,
        "offset=" + useOff,
      ];
      if (groupId !== null && groupId !== undefined) params.push("group_id=" + groupId);
      if (subId !== null && subId !== undefined) params.push("subscription_id=" + subId);
      if (query) params.push("q=" + encodeURIComponent(query));
      return fetchJSON(API + "/board/items?" + params.join("&"))
        .then(function (d) {
          setAvailable(!!d.available);
          var list = d.items || [];
          setTotal(d.total || 0);
          setOffset(useOff + list.length);
          if (reset) setItems(list);
          else setItems(function (prev) { return prev.concat(list); });
        })
        .catch(function (e) {
          setLoadErr("加载失败：" + (e && e.message ? e.message : e));
        })
        .then(function () { setLoading(false); });
    }, [view, groupId, subId, query]);

    useEffect(function () { loadMeta(); }, []);
    useEffect(function () { if (tab === "board") loadItems(true); }, [loadItems, tab]);
    useEffect(function () { if (tab === "summary") loadSummary(); }, [tab]);

    /* ---- 瀑布流：流末尾观察器自动加载下一页 ---- */
    useEffect(function () {
      if (tab !== "board") return;
      var node = document.getElementById("airss-sentinel");
      if (!node || typeof IntersectionObserver === "undefined") return;
      var obs = new IntersectionObserver(function (entries) {
        if (!entries[0].isIntersecting) return;
        var c = refBox.current;
        if (!c.loading && c.items && c.items.length < (c.total || 0)) {
          loadItems(false, c.items.length);
        }
      }, { rootMargin: "600px" });
      obs.observe(node);
      return function () { obs.disconnect(); };
    }, [tab, items.length, total, loadItems]);

    /* ---- 滚到底自动已读：哨兵持续在视口里停满 dwell 后，
       只标当下真正在视口内的未读卡片；图片加载把哨兵顶出视口会自动取消，
       回来重新计时（与 Muse RSS 展板的修法一致）。 ---- */
    useEffect(function () {
      if (tab !== "board" || view !== "unread") return;
      var node = document.getElementById("airss-sentinel");
      if (!node || typeof IntersectionObserver === "undefined") return;
      var timer = null;
      function fire() {
        timer = null;
        var cards = document.querySelectorAll("[data-airss-card]");
        var ids = [];
        var vh = window.innerHeight || document.documentElement.clientHeight;
        cards.forEach(function (card) {
          var r = card.getBoundingClientRect();
          if (r.bottom > 0 && r.top < vh) {
            var id = Number(card.getAttribute("data-airss-card"));
            if (id) ids.push(id);
          }
        });
        var unreadIds = ids.filter(function (id) {
          var it = (refBox.current.items || []).filter(function (x) { return x.id === id; })[0];
          return it && !it.read;
        });
        if (!unreadIds.length) return;
        postJSON(API + "/board/read-many", { ids: unreadIds })
          .then(function () {
            setItems(function (prev) {
              return prev.map(function (it) {
                return unreadIds.indexOf(it.id) >= 0
                  ? Object.assign({}, it, { read: true }) : it;
              });
            });
            setCounts(function (c) {
              return Object.assign({}, c, { unread: Math.max(0, c.unread - unreadIds.length) });
            });
          })
          .catch(function () {});
      }
      var obs = new IntersectionObserver(function (entries) {
        if (entries[0].isIntersecting) {
          if (!timer) timer = setTimeout(fire, BOTTOM_DWELL_MS);
        } else if (timer) {
          clearTimeout(timer);
          timer = null;
        }
      });
      obs.observe(node);
      return function () { obs.disconnect(); if (timer) clearTimeout(timer); };
    }, [tab, view, items.length]);

    /* ---- 就地更新（反馈/已读/收藏共用），不重拉列表 ---- */
    function patchItem(id, patch) {
      setItems(function (prev) {
        return prev.map(function (it) {
          return it.id === id ? Object.assign({}, it, patch) : it;
        });
      });
      setCurrent(function (cur) {
        return cur && cur.id === id ? Object.assign({}, cur, patch) : cur;
      });
      setDetail(function (det) {
        return det && det.id === id ? Object.assign({}, det, patch) : det;
      });
    }

    function onFeedback(item, value, ev) {
      if (ev) ev.stopPropagation();
      var next = item.feedback === value ? 0 : value;
      postJSON(API + "/board/items/" + item.id + "/feedback", { value: next })
        .then(function () { patchItem(item.id, { feedback: next }); })
        .catch(function () {});
    }

    function onStar(item, ev) {
      if (ev) ev.stopPropagation();
      var next = item.starred ? 0 : 1;
      postJSON(API + "/board/items/" + item.id + "/star", { value: next })
        .then(function () {
          patchItem(item.id, { starred: !!next });
          setCounts(function (c) {
            return Object.assign({}, c, { starred: Math.max(0, c.starred + (next ? 1 : -1)) });
          });
        })
        .catch(function () {});
    }

    function onShare(item, ev) {
      if (ev) ev.stopPropagation();
      if (item.link) copyText(item.link).catch(function () {});
    }

    function markReadLocal(item) {
      if (item.read) return;
      postJSON(API + "/board/items/" + item.id + "/read", {})
        .then(function () {
          patchItem(item.id, { read: true });
          setCounts(function (c) {
            return Object.assign({}, c, { unread: Math.max(0, c.unread - 1) });
          });
        })
        .catch(function () {});
    }

    function openItem(item) {
      setCurrent(item);
      setDetail(null);
      fetchJSON(API + "/board/items/" + item.id)
        .then(function (d) { if (d && d.available !== false && d.id) setDetail(d); })
        .catch(function () {});
      markReadLocal(item);
    }

    function closeOverlay() { setCurrent(null); setDetail(null); }

    function onReadAll() {
      var body = {};
      if (subId !== null && subId !== undefined) body.subscription_id = subId;
      if (groupId !== null && groupId !== undefined) body.group_id = groupId;
      postJSON(API + "/board/read-all", body)
        .then(function () {
          loadMeta();
          if (view === "unread") loadItems(true);
          else setItems(function (prev) {
            return prev.map(function (it) { return Object.assign({}, it, { read: true }); });
          });
        })
        .catch(function () {});
    }

    function onRefresh() { loadMeta(); loadItems(true); }

    /* ---- 订阅配置开关（订阅源 / 总结 两页共用） ---- */
    function patchSub(id, patch) {
      setSubs(function (prev) {
        return prev.map(function (s) {
          return s.id === id ? Object.assign({}, s, patch) : s;
        });
      });
      setSummary(function (prev) {
        if (!prev || !prev.groups) return prev;
        return Object.assign({}, prev, {
          groups: prev.groups.map(function (g) {
            return Object.assign({}, g, {
              subscriptions: (g.subscriptions || []).map(function (s) {
                return s.id === id ? Object.assign({}, s, patch) : s;
              }),
            });
          }),
        });
      });
    }

    function onToggleSummary(sub, ev) {
      if (ev) ev.stopPropagation();
      var next = !sub.ai_summary_enabled;
      postJSON(API + "/board/subscriptions/" + sub.id + "/summary", { enabled: next })
        .then(function () { patchSub(sub.id, { ai_summary_enabled: next }); })
        .catch(function () {});
    }

    function onToggleStop(sub, ev) {
      if (ev) ev.stopPropagation();
      var next = !sub.stop;
      postJSON(API + "/board/subscriptions/" + sub.id + "/stop", { stopped: next })
        .then(function () { patchSub(sub.id, { stop: next }); })
        .catch(function () {});
    }

    function switchBtn(on, label, onClick) {
      return el("button", {
        style: Object.assign({}, S.switchBtn, on ? S.switchOn : null),
        onClick: onClick,
      }, label + (on ? " · 开" : " · 关"));
    }

    /* ---- 条目渲染 ---- */
    function actionButtons(item) {
      return el("div", { style: S.fbRow },
        el("button", {
          style: Object.assign({}, S.fbBtn, item.feedback === 1 ? S.fbOn : null),
          onClick: function (e) { onFeedback(item, 1, e); },
        }, item.feedback === 1 ? "♥ 已喜欢" : "♡ 喜欢"),
        el("button", {
          style: Object.assign({}, S.fbBtn, item.feedback === -1 ? S.fbOn : null),
          onClick: function (e) { onFeedback(item, -1, e); },
        }, item.feedback === -1 ? "已不感兴趣" : "不感兴趣"),
        el("button", {
          style: Object.assign({}, S.fbBtn, item.starred ? S.fbOn : null),
          onClick: function (e) { onStar(item, e); },
        }, item.starred ? "★ 已收藏" : "☆ 收藏"),
        item.link ? el("button", {
          style: S.fbBtn,
          onClick: function (e) { onShare(item, e); },
        }, "分享") : null
      );
    }

    function metaBits(item) {
      return [
        el("span", { key: "s" }, item.subscription_name || ""),
        el("span", { key: "t" }, fmtTime(item)),
        item.ai_filter_result === 0
          ? el("span", { key: "ai", style: S.aiBadge }, "AI 已过滤")
          : null,
      ];
    }

    function renderCard(item) {
      var children = [];
      if (item.image) {
        children.push(el("img", {
          key: "img", src: item.image, loading: "lazy", alt: "",
          style: { width: "100%", height: "auto", display: "block" },
        }));
      }
      children.push(el("div", {
        key: "b", style: Object.assign({}, S.cardBody, item.read ? { opacity: 0.55 } : null),
      }, [
        el("div", { key: "t", style: S.cardTitle }, item.title || "(无标题)"),
        el("div", { key: "m", style: S.metaLine }, metaBits(item)),
        el("div", { key: "f" }, actionButtons(item)),
      ]));
      return el("div", {
        key: item.id, style: S.card, "data-airss-card": item.id,
        onClick: function () { openItem(item); },
      }, children);
    }

    function renderRow(item) {
      return el("div", {
        key: item.id, style: Object.assign({}, S.row, item.read ? { opacity: 0.55 } : null),
        "data-airss-card": item.id,
        onClick: function () { openItem(item); },
      },
        item.image ? el("img", {
          src: item.image, loading: "lazy", alt: "",
          style: { width: 84, height: 84, objectFit: "cover", borderRadius: 8, flexShrink: 0 },
        }) : null,
        el("div", { style: { flex: 1, display: "flex", flexDirection: "column", gap: 6 } },
          el("div", { style: S.cardTitle }, item.title || "(无标题)"),
          el("div", { style: S.metaLine }, metaBits(item)),
          actionButtons(item)
        )
      );
    }

    function renderOverlay() {
      if (!current) return null;
      var d = detail && detail.id === current.id ? detail : current;
      var images = (detail && detail.image_urls && detail.image_urls.length)
        ? detail.image_urls
        : (current.image ? [current.image] : []);
      var kids = [
        el("div", {
          key: "hd",
          style: { display: "flex", justifyContent: "space-between", gap: 12, alignItems: "flex-start" },
        },
          el("h2", { style: { fontSize: 19, lineHeight: 1.45, margin: 0 } }, d.title || "(无标题)"),
          el("button", { style: S.btn, onClick: closeOverlay }, "关闭")
        ),
        el("div", { key: "mt", style: Object.assign({}, S.metaLine, { marginTop: 8 }) },
          el("span", null, d.subscription_name || ""),
          d.author ? el("span", null, d.author) : null,
          el("span", null, fmtTime(d)),
          d.ai_filter_result === 0 ? el("span", { style: S.aiBadge }, "AI 已过滤") : null
        ),
      ];
      images.forEach(function (src, i) {
        kids.push(el("img", {
          key: "im" + i, src: src, alt: "", loading: "lazy",
          style: { width: "100%", height: "auto", display: "block", borderRadius: 8, marginTop: 12 },
        }));
      });
      if (detail && detail.id === current.id) {
        if (detail.content) {
          kids.push(el("div", {
            key: "ct",
            style: { marginTop: 14, lineHeight: 1.7, wordBreak: "break-word" },
            dangerouslySetInnerHTML: { __html: detail.content },
          }));
        } else {
          kids.push(el("p", { key: "nc", style: { marginTop: 14, opacity: 0.7 } },
            "这个源没有给正文，点下方「在浏览器打开原文」看全文。"));
        }
      } else {
        kids.push(el("p", { key: "ld", style: { marginTop: 14, opacity: 0.7 } }, "正文加载中…"));
      }
      kids.push(el("div", { key: "fb", style: { marginTop: 16 } }, actionButtons(d)));
      kids.push(el("div", {
        key: "ft",
        style: { display: "flex", gap: 10, marginTop: 14, flexWrap: "wrap" },
      },
        d.link
          ? el("a", {
              href: d.link, target: "_blank", rel: "noreferrer",
              style: Object.assign({}, S.btn, { textDecoration: "none", display: "inline-block" }),
            }, "在浏览器打开原文")
          : null,
        el("button", { style: S.btn, onClick: closeOverlay }, "关闭")
      ));
      return el("div", {
        style: S.overlayBg,
        onClick: function (e) { if (e.target === e.currentTarget) closeOverlay(); },
      }, el("div", { style: S.panel }, kids));
    }

    /* ---- 展板页 ---- */
    function renderBoardTab() {
      var header = el("div", { style: S.header },
        el("span", { style: S.title }, "RSS 展板"),
        counts.unread > 0 ? el("span", { style: S.unreadBadge }, "未读 " + counts.unread) : null,
        el("span", { style: { fontSize: 12, opacity: 0.6 } }, "共 " + counts.total + " 篇")
      );

      var viewDefs = [
        ["unread", "未读"],
        ["all", "全部"],
        ["starred", "收藏" + (counts.starred ? " " + counts.starred : "")],
        ["filtered", "已过滤" + (counts.filtered ? " " + counts.filtered : "")],
      ];
      var viewToggle = el("div", { style: { display: "flex", gap: 6, flexWrap: "wrap" } },
        viewDefs.map(function (vd) {
          return el("button", {
            key: vd[0],
            style: Object.assign({}, S.chip, view === vd[0] ? S.chipOn : null),
            onClick: function () { setView(vd[0]); },
          }, vd[1]);
        })
      );

      var groupChips = [el("button", {
        key: "all",
        style: Object.assign({}, S.chip, groupId === null ? S.chipOn : null),
        onClick: function () { setGroupId(null); setSubId(null); },
      }, "全部分组")].concat(groups.map(function (g) {
        return el("button", {
          key: g.id,
          style: Object.assign({}, S.chip, groupId === g.id ? S.chipOn : null),
          onClick: function () { setGroupId(g.id); setSubId(null); },
        }, g.name + (g.unread ? " · " + g.unread : ""));
      }));

      var subOptions = [el("option", { key: "all", value: "" }, "全部订阅源")].concat(
        subs.filter(function (s) { return groupId === null || s.group_id === groupId; })
          .map(function (s) { return el("option", { key: s.id, value: String(s.id) }, s.name); })
      );

      var toolbar = el("div", null,
        el("div", { style: S.toolbar }, viewToggle, el("span", { style: { width: 6 } }), groupChips),
        el("div", { style: S.toolbar },
          el("select", {
            value: subId === null ? "" : String(subId),
            onChange: function (e) { setSubId(e.target.value === "" ? null : Number(e.target.value)); },
            style: { padding: "5px 8px", borderRadius: 8, fontSize: 13, maxWidth: 220 },
          }, subOptions),
          el("input", {
            value: qInput, placeholder: "搜索标题/作者，回车确认",
            onChange: function (e) { setQInput(e.target.value); },
            onKeyDown: function (e) { if (e.key === "Enter") setQuery(qInput); },
            style: Object.assign({}, S.input, { width: 190 }),
          }),
          el("button", { style: S.btn, onClick: function () { setQuery(qInput); } }, "搜索"),
          el("button", {
            style: Object.assign({}, S.chip, layout === "grid" ? S.chipOn : null),
            onClick: function () { setLayout("grid"); },
          }, "海报墙"),
          el("button", {
            style: Object.assign({}, S.chip, layout === "list" ? S.chipOn : null),
            onClick: function () { setLayout("list"); },
          }, "列表"),
          el("button", { style: S.btn, onClick: onReadAll }, "全部标已读"),
          el("button", { style: S.btn, onClick: onRefresh }, "刷新")
        )
      );

      var main;
      if (available === false) {
        main = el("div", { style: S.empty },
          "airss 数据库还没生成（rss.db 不存在）。先让 airss 抓取一次，展板就会有数据。");
      } else if (loadErr) {
        main = el("div", { style: S.empty }, loadErr,
          el("div", { style: { marginTop: 10 } },
            el("button", { style: S.btn, onClick: onRefresh }, "重试")));
      } else if (!items.length && !loading) {
        main = el("div", { style: S.empty },
          view === "unread" ? "暂无未读条目，都看完啦。"
            : view === "starred" ? "还没有收藏的条目。"
            : view === "filtered" ? "没有被 AI 过滤的条目。"
            : "没有符合条件的条目。");
      } else {
        main = el("div", null,
          layout === "grid"
            ? el("div", { style: S.grid }, items.map(renderCard))
            : el("div", { style: S.listWrap }, items.map(renderRow)),
          loading ? el("div", { style: S.empty }, "加载中…") : null
        );
      }

      return el("div", null,
        header, toolbar, main,
        el("div", { id: "airss-sentinel", style: { height: 8 } }),
        view === "unread" && items.length > 0
          ? el("div", { style: Object.assign({}, S.note, { textAlign: "center", padding: "6px 0 14px" }) },
              "停在底部约 2.5 秒，当前屏幕这一屏未读会自动标已读")
          : null,
        renderOverlay()
      );
    }

    /* ---- 订阅源页 ---- */
    function renderSubsTab() {
      var groupName = {};
      groups.forEach(function (g) { groupName[g.id] = g.name; });
      if (available === false) {
        return el("div", { style: S.empty }, "airss 数据库还没生成（rss.db 不存在）。");
      }
      if (!subs.length) return el("div", { style: S.empty }, "还没有订阅源。");
      return el("div", null,
        el("div", { style: Object.assign({}, S.note, { marginBottom: 10 }) },
          "总结开关：开 = 该订阅的文章留给所在分组的定时总结，不逐条推送；关 = 逐条推送/直接阅读。" +
          "总结的排期与提示词在「总结」页里配。暂停开关停掉抓取与推送。"),
        el("div", { style: S.listWrap }, subs.map(function (s) {
          return el("div", { key: s.id, style: S.subRow },
            el("div", { style: { flex: 1, minWidth: 220 } },
              el("div", { style: { fontWeight: 600, fontSize: 14 } },
                s.name,
                s.unread ? el("span", {
                  style: Object.assign({}, S.unreadBadge, { marginLeft: 8 }),
                }, "未读 " + s.unread) : null,
                s.error_count > 0 ? el("span", {
                  style: Object.assign({}, S.aiBadge, { marginLeft: 8 }),
                }, "失败 " + s.error_count + " 次") : null
              ),
              el("div", { style: S.note },
                (groupName[s.group_id] || "默认分组") + " · 每 " + s.interval + " 分钟 · " +
                (s.last_fetch ? "上次抓取 " + String(s.last_fetch).replace("T", " ").slice(0, 16) : "还没抓过")),
              el("div", {
                style: {
                  fontSize: 11, opacity: 0.5, wordBreak: "break-all",
                },
              }, s.url)
            ),
            el("div", { style: { display: "flex", gap: 8, flexWrap: "wrap" } },
              switchBtn(s.ai_summary_enabled, "总结", function (e) { onToggleSummary(s, e); }),
              switchBtn(!s.stop, "抓取", function (e) { onToggleStop(s, e); })
            )
          );
        }))
      );
    }

    /* ---- 总结页 ---- */
    function PromptEditor(props) {
      // props: {initial, placeholder, onSave(text), saveLabel, draftKey}
      // 草稿存 refBox：父级重渲染导致组件重挂载时，未保存的输入不丢。
      var draftKey = props.draftKey || "prompt";
      var st = useState(function () {
        var d = refBox.current["draft:" + draftKey];
        return d !== undefined ? d : (props.initial || "");
      });
      var text = st[0], setText = st[1];
      var savedState = useState(false);
      var saved = savedState[0], setSaved = savedState[1];
      return el("div", null,
        el("textarea", {
          style: S.textarea, value: text,
          placeholder: props.placeholder || "",
          onChange: function (e) {
            setText(e.target.value);
            refBox.current["draft:" + draftKey] = e.target.value;
            setSaved(false);
          },
        }),
        el("div", { style: { display: "flex", gap: 8, marginTop: 6, flexWrap: "wrap" } },
          el("button", {
            style: S.btn,
            onClick: function () {
              props.onSave(text.trim()).then(function () {
                delete refBox.current["draft:" + draftKey];
                setSaved(true);
              });
            },
          }, props.saveLabel || "保存"),
          props.onClear ? el("button", {
            style: S.btn,
            onClick: function () {
              setText("");
              delete refBox.current["draft:" + draftKey];
              props.onClear().then(function () { setSaved(true); });
            },
          }, "清除（用上一级）") : null,
          saved ? el("span", { style: { fontSize: 12, opacity: 0.7, alignSelf: "center" } }, "已保存") : null
        )
      );
    }

    function renderSummaryTab() {
      if (available === false) {
        return el("div", { style: S.empty }, "airss 数据库还没生成（rss.db 不存在）。");
      }
      if (sumLoading && !summary) return el("div", { style: S.empty }, "加载中…");
      if (!summary || !summary.available) {
        return el("div", { style: S.empty }, "总结配置暂时读不到，稍后刷新重试。");
      }
      var sourceLabel = { group: "本分组自定义", global: "面板全局", default: "内置默认" };
      var kids = [
        el("div", { key: "intro", style: Object.assign({}, S.note, { marginBottom: 12 }) },
          "分组总结沿用 airss 原有机制：分组设了排期时间后，组内「已启用总结」的订阅文章会攒着，" +
          "到点由 AI 按提示词汇总成一份日报推送；未启用的订阅照常逐条推送。" +
          "提示词优先级：分组自定义 > 面板全局 > 内置默认。订阅的总结开关也可以在「订阅源」页改。"),
        el("div", { key: "global", style: S.groupCard },
          el("div", { style: { fontWeight: 700, fontSize: 15 } }, "面板全局提示词"),
          el("div", { style: S.note }, "对没有分组自定义提示词的分组生效。留空保存 = 清除，回落到内置默认。"),
          el(PromptEditor, {
            key: "gp-" + (summary.global_prompt ? "set" : "empty"),
            draftKey: "global-prompt",
            initial: summary.global_prompt || "",
            placeholder: "留空则使用内置默认提示词（资深资讯编辑日报格式）",
            saveLabel: "保存全局提示词",
            onSave: function (text) {
              return postJSON(API + "/board/summary/global-prompt", { prompt: text })
                .then(function () { loadSummary(); loadMeta(); });
            },
          }),
          summary.default_prompt ? el("details", { key: "def", style: { marginTop: 4 } },
            el("summary", { style: { fontSize: 12, cursor: "pointer", opacity: 0.75 } }, "查看内置默认提示词"),
            el("pre", {
              style: {
                whiteSpace: "pre-wrap", fontSize: 12, lineHeight: 1.6, opacity: 0.8,
                border: "1px solid rgba(127,127,127,.3)", borderRadius: 8, padding: 10, marginTop: 6,
              },
            }, summary.default_prompt)
          ) : null
        ),
      ];
      (summary.groups || []).forEach(function (g) {
        var schedStateKey = "sched-" + g.id;
        kids.push(el(GroupSummaryCard, {
          key: g.id, group: g, sourceLabel: sourceLabel,
          schedStateKey: schedStateKey,
        }));
      });
      return el("div", null, kids);
    }

    function GroupSummaryCard(props) {
      var g = props.group;
      var schedSt = useState(function () {
        var d = refBox.current["draft:sched-" + g.id];
        return d !== undefined ? d : (g.schedules || []).join(", ");
      });
      var schedText = schedSt[0], setSchedText = schedSt[1];
      var schedSavedSt = useState(false);
      var schedSaved = schedSavedSt[0], setSchedSaved = schedSavedSt[1];
      var enabledCount = (g.subscriptions || []).filter(function (s) { return s.ai_summary_enabled; }).length;
      return el("div", { style: S.groupCard },
        el("div", { style: { display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap" } },
          el("span", { style: { fontWeight: 700, fontSize: 15 } }, g.name),
          el("span", { style: S.srcBadge }, "提示词来源：" + props.sourceLabel[g.prompt_source || "default"]),
          el("span", { style: S.note }, "已启用总结 " + enabledCount + " / " + (g.subscriptions || []).length + " 个订阅")
        ),
        el("div", null,
          el("div", { style: Object.assign({}, S.note, { marginBottom: 4 }) },
            "总结排期（逗号分隔，HH:MM 或 5 段 cron；留空 = 不定时总结，文章逐条推送）"),
          el("div", { style: { display: "flex", gap: 8, flexWrap: "wrap" } },
            el("input", {
              style: Object.assign({}, S.input, { flex: 1, minWidth: 220 }),
              value: schedText, placeholder: "如 08:00, 20:30",
              onChange: function (e) {
                setSchedText(e.target.value);
                refBox.current["draft:sched-" + g.id] = e.target.value;
                setSchedSaved(false);
              },
            }),
            el("button", {
              style: S.btn,
              onClick: function () {
                var list = schedText.split(",").map(function (x) { return x.trim(); })
                  .filter(function (x) { return x; });
                postJSON(API + "/board/summary/group-schedules", { group_id: g.id, schedules: list })
                  .then(function () {
                    delete refBox.current["draft:sched-" + g.id];
                    setSchedSaved(true);
                    loadSummary();
                  })
                  .catch(function () {});
              },
            }, "保存排期"),
            schedSaved ? el("span", { style: { fontSize: 12, opacity: 0.7, alignSelf: "center" } }, "已保存") : null
          )
        ),
        el("div", null,
          el("div", { style: Object.assign({}, S.note, { marginBottom: 4 }) }, "本分组自定义提示词"),
          el(PromptEditor, {
            key: "grp-" + g.id + "-" + (g.persona_prompt ? "set" : "empty"),
            draftKey: "group-prompt-" + g.id,
            initial: g.persona_prompt || "",
            placeholder: "留空则使用面板全局提示词 / 内置默认",
            saveLabel: "保存分组提示词",
            onSave: function (text) {
              return postJSON(API + "/board/summary/group-prompt", { group_id: g.id, prompt: text })
                .then(function () { loadSummary(); });
            },
            onClear: function () {
              return postJSON(API + "/board/summary/group-prompt", { group_id: g.id, prompt: "" })
                .then(function () { loadSummary(); });
            },
          })
        ),
        (g.subscriptions || []).length ? el("div", null,
          el("div", { style: Object.assign({}, S.note, { marginBottom: 4 }) }, "组内订阅的总结开关"),
          el("div", { style: { display: "flex", flexDirection: "column", gap: 6 } },
            (g.subscriptions || []).map(function (s) {
              var full = subs.filter(function (x) { return x.id === s.id; })[0] || s;
              return el("div", {
                key: s.id,
                style: { display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap" },
              },
                el("span", { style: { fontSize: 13, flex: 1, minWidth: 160 } }, s.name),
                switchBtn(full.ai_summary_enabled, "总结", function (e) { onToggleSummary(full, e); })
              );
            }))
        ) : null
      );
    }

    /* ---- 页面主体 ---- */
    var tabsBar = el("div", { style: S.tabs },
      [["board", "展板"], ["subs", "订阅源"], ["summary", "总结"]].map(function (td) {
        return el("button", {
          key: td[0],
          style: Object.assign({}, S.tab, tab === td[0] ? S.tabOn : null),
          onClick: function () { setTab(td[0]); },
        }, td[1]);
      }),
      tab === "board" && counts.unread > 0
        ? el("span", { style: Object.assign({}, S.unreadBadge, { alignSelf: "center" }) }, "未读 " + counts.unread)
        : null
    );

    var body = tab === "board" ? renderBoardTab()
      : tab === "subs" ? renderSubsTab()
      : renderSummaryTab();

    return el("div", { style: S.page }, tabsBar, body);
  }

  registry.register("airss", BoardPage);
})();
