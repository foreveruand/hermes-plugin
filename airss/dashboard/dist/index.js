/* airss「RSS 展板」仪表盘 UI 插件（无构建、纯 IIFE）。
 * 数据经本插件后端 /api/plugins/airss/board/* 读 airss 本地库；
 * 已读/反馈只写后端 board_state 表，不碰 airss 的推送语义字段。
 * 关键约定：点喜欢/不感兴趣/标已读只就地更新本地 state，
 * 绝不重新拉整个列表（整表刷新会丢失滚动位置）。
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

  var S = {
    page: { padding: 16, maxWidth: 1100, margin: "0 auto" },
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
    card: {
      border: "1px solid rgba(127,127,127,.3)", borderRadius: 10, overflow: "hidden",
      cursor: "pointer", display: "flex", flexDirection: "column",
    },
    cardBody: { padding: "10px 12px", display: "flex", flexDirection: "column", gap: 6, flex: 1 },
    cardTitle: { fontSize: 15, fontWeight: 600, lineHeight: 1.4 },
    metaLine: { fontSize: 12, opacity: 0.65, display: "flex", gap: 8, flexWrap: "wrap" },
    fbRow: { display: "flex", gap: 8, marginTop: "auto", paddingTop: 4 },
    fbBtn: {
      padding: "3px 10px", borderRadius: 999, fontSize: 12, cursor: "pointer",
      border: "1px solid rgba(127,127,127,.4)", background: "transparent",
    },
    fbOn: { background: "rgba(127,127,127,.30)", fontWeight: 700 },
    aiBadge: {
      fontSize: 11, padding: "1px 7px", borderRadius: 999,
      background: "rgba(200,140,0,.25)", border: "1px solid rgba(200,140,0,.6)",
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
  };

  function BoardPage() {
    var availState = useState(null); // null=未知 true/false
    var available = availState[0], setAvailable = availState[1];
    var countsState = useState({ total: 0, unread: 0 });
    var counts = countsState[0], setCounts = countsState[1];
    var groupsState = useState([]);
    var groups = groupsState[0], setGroups = groupsState[1];
    var subsState = useState([]);
    var subs = subsState[0], setSubs = subsState[1];

    var viewState = useState("unread");
    var view = viewState[0], setView = viewState[1];
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

    var curState = useState(null); // 阅读层里的条目（先用列表数据占位）
    var current = curState[0], setCurrent = curState[1];
    var detState = useState(null); // 阅读层详情（含正文）
    var detail = detState[0], setDetail = detState[1];

    function loadMeta() {
      return fetchJSON(API + "/board/meta")
        .then(function (d) {
          setAvailable(!!d.available);
          if (d.available) {
            setCounts(d.counts || { total: 0, unread: 0 });
            setGroups(d.groups || []);
            setSubs(d.subscriptions || []);
          }
        })
        .catch(function () { setAvailable(false); });
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
    useEffect(function () { loadItems(true); }, [loadItems]);

    // 只就地更新一条（反馈/已读共用），不重拉列表
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
        .catch(function () { /* 失败保持原状 */ });
    }

    function markReadLocal(item) {
      if (item.read) return;
      postJSON(API + "/board/items/" + item.id + "/read", {})
        .then(function () {
          patchItem(item.id, { read: true });
          setCounts(function (c) {
            return { total: c.total, unread: Math.max(0, c.unread - 1) };
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

    function feedbackButtons(item) {
      return el("div", { style: S.fbRow },
        el("button", {
          style: Object.assign({}, S.fbBtn, item.feedback === 1 ? S.fbOn : null),
          onClick: function (e) { onFeedback(item, 1, e); },
        }, item.feedback === 1 ? "♥ 已喜欢" : "♡ 喜欢"),
        el("button", {
          style: Object.assign({}, S.fbBtn, item.feedback === -1 ? S.fbOn : null),
          onClick: function (e) { onFeedback(item, -1, e); },
        }, item.feedback === -1 ? "已不感兴趣" : "不感兴趣")
      );
    }

    function renderCard(item) {
      var children = [];
      if (item.image) {
        children.push(el("img", {
          key: "img", src: item.image, loading: "lazy", alt: "",
          style: { width: "100%", height: "auto", display: "block" },
        }));
      }
      var bodyChildren = [
        el("div", { key: "t", style: S.cardTitle }, item.title || "(无标题)"),
        el("div", { key: "m", style: S.metaLine },
          el("span", null, item.subscription_name || ""),
          el("span", null, fmtTime(item)),
          item.ai_filter_result === 0
            ? el("span", { style: S.aiBadge }, "AI 已过滤")
            : null
        ),
        el("div", { key: "f" }, feedbackButtons(item)),
      ];
      children.push(el("div", {
        key: "b", style: Object.assign({}, S.cardBody, item.read ? { opacity: 0.55 } : null),
      }, bodyChildren));
      return el("div", {
        key: item.id, style: S.card,
        onClick: function () { openItem(item); },
      }, children);
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
      kids.push(el("div", { key: "fb", style: { marginTop: 16 } }, feedbackButtons(d)));
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

    // ---- 页面主体 ----
    var header = el("div", { style: S.header },
      el("span", { style: S.title }, "RSS 展板"),
      counts.unread > 0 ? el("span", { style: S.unreadBadge }, "未读 " + counts.unread) : null,
      el("span", { style: { fontSize: 12, opacity: 0.6 } }, "共 " + counts.total + " 篇")
    );

    var viewToggle = el("div", { style: { display: "flex", gap: 6 } },
      el("button", {
        style: Object.assign({}, S.chip, view === "unread" ? S.chipOn : null),
        onClick: function () { setView("unread"); },
      }, "未读"),
      el("button", {
        style: Object.assign({}, S.chip, view === "all" ? S.chipOn : null),
        onClick: function () { setView("all"); },
      }, "全部")
    );

    var groupChips = [el("button", {
      key: "all",
      style: Object.assign({}, S.chip, groupId === null ? S.chipOn : null),
      onClick: function () { setGroupId(null); },
    }, "全部分组")].concat(groups.map(function (g) {
      return el("button", {
        key: g.id,
        style: Object.assign({}, S.chip, groupId === g.id ? S.chipOn : null),
        onClick: function () { setGroupId(g.id); },
      }, g.name);
    }));

    var subOptions = [el("option", { key: "all", value: "" }, "全部订阅源")].concat(
      subs.filter(function (s) { return groupId === null || s.group_id === groupId; })
        .map(function (s) { return el("option", { key: s.id, value: String(s.id) }, s.name); })
    );

    var toolbar = el("div", null,
      el("div", { style: S.toolbar },
        viewToggle,
        el("span", { style: { width: 6 } }),
        groupChips,
      ),
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
          style: { padding: "5px 9px", borderRadius: 8, fontSize: 13, width: 190 },
        }),
        el("button", { style: S.btn, onClick: function () { setQuery(qInput); } }, "搜索"),
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
        view === "unread" ? "暂无未读条目，都看完啦。" : "没有符合条件的条目。");
    } else {
      main = el("div", null,
        el("div", { style: S.grid }, items.map(renderCard)),
        loading ? el("div", { style: S.empty }, "加载中…") : null,
        (!loading && items.length < total)
          ? el("div", { style: { textAlign: "center", marginTop: 16 } },
              el("button", { style: S.btn, onClick: function () { loadItems(false, offset); } },
                "加载更多（" + items.length + " / " + total + "）"))
          : null
      );
    }

    return el("div", { style: S.page }, header, toolbar, main, renderOverlay());
  }

  registry.register("airss", BoardPage);
})();
