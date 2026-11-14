import {
  React,
  useCallback,
  useEffect,
  useState,
} from "./sdk.js";
import { addCharacter, fetchPortrait, getBootstrap, getTeams, renderNote, setFavorites } from "./api.js";
import { Wall } from "./wall.jsx";
import { Detail, storeDetail } from "./detail.jsx";
import { Teams } from "./teams.jsx";
import { summaryFromPayload } from "./util.js";

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
        characters: previous.characters.map((character) =>
          character.name === name ? { ...character, ...patch } : character
        ),
      };
    });
  }, []);

  const applyPayload = useCallback((payload) => {
    // Keep the wall card and the revision in sync without refetching all of
    // /bootstrap after a single-character mutation.
    setData((previous) => {
      if (!previous) return previous;
      const summary = summaryFromPayload(payload);
      const nextFavorites = payload.favorite
        ? Array.from(new Set([...(previous.favorites || []), payload.character.name]))
        : (previous.favorites || []).filter((item) => item !== payload.character.name);
      return {
        ...previous,
        rev: payload.rev,
        targets_rev: payload.targets_rev || previous.targets_rev,
        favorites: nextFavorites,
        counts: {
          ...previous.counts,
          favorites: nextFavorites.length,
          owned: payload.created
            ? (previous.counts.owned || 0) + 1
            : previous.counts.owned,
        },
        characters: previous.characters.map((character) =>
          character.name === payload.character.name
            ? { ...character, ...summary, owned: true }
            : character
        ),
      };
    });
  }, []);

  const refreshPortrait = useCallback(async (name, notify) => {
    try {
      const result = await fetchPortrait(name);
      if (result && result.portrait && result.portrait.has_cache) {
        if (notify) toast(result.fetched ? "头像已补上" : "头像已就绪", "ok");
        patchCharacter(result.name || name, { portrait: result.portrait });
      } else if (notify && result && !result.fetched) {
        toast("未找到头像，可在「校核」里填写头像名称", "warn");
      }
      return result;
    } catch (err) {
      if (notify) toast(err.message || "头像抓取失败", "warn");
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
        toast("已自动加入看板", "ok");
        storeDetail(payload);
        applyPayload(payload);
        setDetailName(payload.character.name);
        refreshPortrait(payload.character.name, false);
      } catch (err) {
        toast(err.message || "自动加入失败", "error");
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
    const nextFavorites = next
      ? Array.from(new Set([...current, name]))
      : current.filter((item) => item !== name);
    setData((previous) => ({
      ...previous,
      favorites: nextFavorites,
      characters: previous.characters.map((character) =>
        character.name === name ? { ...character, favorite: next } : character
      ),
    }));
    try {
      await setFavorites(nextFavorites);
      return true;
    } catch (err) {
      setData((previous) => ({
        ...previous,
        favorites: current,
        characters: previous.characters.map((character) =>
          character.name === name ? { ...character, favorite: !next } : character
        ),
      }));
      toast(err.message || "收藏失败", "error");
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
    toast("已加入看板", "ok");
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
        result.ok
          ? result.changed
            ? "已同步 nikke.md"
            : "Markdown 无变化"
          : "渲染失败：" + (result.error || ""),
        result.ok ? "ok" : "warn"
      );
      if (result.ok) loadBootstrap();
    } catch (err) {
      toast(err.message || "渲染失败", "error");
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

  return (
    <div className="nt-root">
      <header className="nt-topbar">
        <div className="nt-brand">
          <h1>NIKKE 养成</h1>
          <p>装备词条 · 洗条建议 · 配队速查</p>
        </div>
        <nav className="nt-nav">
          <button
            type="button"
            className={"nt-nav-btn" + (isWall ? " is-active" : "")}
            onClick={showWall}
          >
            角色墙
          </button>
          <button
            type="button"
            className={"nt-nav-btn" + (isTeams ? " is-active" : "")}
            onClick={showTeams}
          >
            配队速查
          </button>
          <button type="button" className="nt-nav-btn" onClick={loadBootstrap} disabled={loading}>
            {loading ? "刷新中…" : "刷新"}
          </button>
        </nav>
      </header>

      {isWall ? (
        <Wall
          data={data}
          loading={loading}
          error={error}
          onRetry={loadBootstrap}
          onOpen={openDetail}
          openingName={openingName}
          onToggleFavorite={toggleFavorite}
          onRender={handleRender}
          rendering={rendering}
        />
      ) : null}

      {isTeams ? (
        <Teams data={teams} loading={teamsLoading} error={teamsError} onRetry={showTeams} />
      ) : null}

      {detailName ? (
        <Detail
          key={detailName}
          name={detailName}
          bootstrap={data}
          onClose={closeDetail}
          onSaved={handleSaved}
          onAdd={handleAdd}
          onFetchPortrait={refreshPortrait}
          onTargetSaved={handleTargetSaved}
          onPortraitUploaded={handlePortraitUploaded}
          onToggleFavorite={toggleFavorite}
          toast={toast}
        />
      ) : null}

      <div className="nt-toasts" aria-live="polite">
        {toasts.map((item) => (
          <div key={item.id} className={"nt-toast nt-toast-" + item.tone}>
            {item.message}
          </div>
        ))}
      </div>
    </div>
  );
}

window.__HERMES_PLUGINS__.register("nikke-tracker", App);