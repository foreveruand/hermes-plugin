import {
  React,
  useMemo,
  useState,
} from "./sdk.js";
import { Portrait } from "./portrait.jsx";
import {
  elementColor,
  favoriteQueue,
  percent,
  scoreColor,
  sortCharacters,
  cleanAdvice,
} from "./util.js";

function Ring({ score, grade, size = 48, stroke = 4 }) {
  const pct = percent(score);
  if (pct === null) return null;
  const radius = (size - stroke) / 2;
  const circumference = 2 * Math.PI * radius;
  const filled = Math.max(0, Math.min(1, Number(score))) * circumference;
  return (
    <div className="nt-ring" title={"装备成型度 " + pct + "%" + (grade ? " · " + grade : "")}>
      <svg width={size} height={size} viewBox={"0 0 " + size + " " + size}>
        <circle
          cx={size / 2}
          cy={size / 2}
          r={radius}
          fill="none"
          stroke="rgba(255,255,255,0.16)"
          strokeWidth={stroke}
        />
        <circle
          cx={size / 2}
          cy={size / 2}
          r={radius}
          fill="none"
          stroke={scoreColor(score)}
          strokeWidth={stroke}
          strokeDasharray={filled + " " + circumference}
          strokeLinecap="round"
          transform={"rotate(-90 " + size / 2 + " " + size / 2 + ")"}
        />
      </svg>
      <span className="nt-ring-text">{pct}%</span>
    </div>
  );
}

function TierBadge({ tier }) {
  if (!tier) return null;
  return (
    <span className={"nt-tier nt-tier-" + tier} title={"梯度 " + tier}>
      {tier}
    </span>
  );
}

const CharacterCard = React.memo(function CharacterCardImpl({ character, onOpen, onToggleFavorite, opening }) {
  const c = character;
  const score = percent(c.score);
  return (
    <div
      className={"nt-card" + (c.owned ? "" : " is-unowned") + (opening ? " is-opening" : "")}
      role="button"
      tabIndex={0}
      onClick={() => {
        if (!opening) onOpen(c.name);
      }}
      onKeyDown={(event) => {
        if (event.key === "Enter" || event.key === " ") {
          event.preventDefault();
          if (!opening) onOpen(c.name);
        }
      }}
      title={c.owned ? cleanAdvice(c.priority) || c.role : "尚未加入看板，点击会自动加入并打开详情"}
    >
      <div className="nt-card-art">
        <Portrait
          name={c.name}
          element={c.element}
          portrait={c.portrait}
          className="nt-card-portrait"
        />
        <div className="nt-card-shade" />
        <TierBadge tier={c.tier} />
        {c.owned ? (
          <button
            type="button"
            className={"nt-star" + (c.favorite ? " is-on" : "")}
            aria-label={c.favorite ? "取消收藏" : "收藏"}
            title={c.favorite ? "取消收藏" : "加入养成队列"}
            onClick={(event) => {
              event.stopPropagation();
              onToggleFavorite(c.name, !c.favorite);
            }}
          >
            {c.favorite ? "★" : "☆"}
          </button>
        ) : null}
        {c.owned ? (
          <Ring score={c.score} grade={c.grade} />
        ) : (
          <span className="nt-unowned-badge">点击加入</span>
        )}
        {opening ? <span className="nt-opening">加入中…</span> : null}
        <span className="nt-element-dot" style={{ background: elementColor(c.element) }} />
      </div>
      <div className="nt-card-body">
        <div className="nt-card-name" title={c.name}>{c.name}</div>
        <div className="nt-card-meta">
          {c.element} · {c.class} · {c.burst}
        </div>
        <div className="nt-card-foot">
          {c.owned ? (
            <>
              <span className={"nt-grade nt-grade-" + (c.grade || "unknown")}>{c.grade || "—"}</span>
              <span className="nt-affix-count">词条 {c.effective}</span>
            </>
          ) : (
            <span className="nt-muted">点击加入并查看</span>
          )}
        </div>
      </div>
    </div>
  );
});

function Queue({ characters, onOpen }) {
  const queue = favoriteQueue(characters).slice(0, 5);
  if (!queue.length) {
    return (
      <section className="nt-queue nt-queue-empty">
        <div className="nt-queue-title">养成队列</div>
        <div className="nt-muted">点卡片左上角的星标，把想练的角色排进队列。</div>
      </section>
    );
  }
  return (
    <section className="nt-queue">
      <div className="nt-queue-head">
        <div>
          <div className="nt-queue-title">养成队列</div>
          <div className="nt-muted">按梯度和装备成型度自动排序，先练最需要的。</div>
        </div>
        <span className="nt-queue-count">{queue.length} 名</span>
      </div>
      <div className="nt-queue-list">
        {queue.map((c, index) => (
          <button
            key={c.name}
            type="button"
            className="nt-queue-item"
            onClick={() => onOpen(c.name)}
          >
            <span className="nt-queue-rank">{index + 1}</span>
            <span className={"nt-tier nt-tier-" + c.tier}>{c.tier}</span>
            <span className="nt-queue-name">{c.name}</span>
            <span className="nt-queue-score">{percent(c.score)}%</span>
            <span className="nt-queue-arrow">›</span>
          </button>
        ))}
      </div>
    </section>
  );
}

function SkeletonCard() {
  return (
    <div className="nt-card nt-skeleton">
      <div className="nt-skeleton-art" />
      <div className="nt-skeleton-line" />
      <div className="nt-skeleton-line short" />
    </div>
  );
}

export function Wall({ data, loading, error, onRetry, onOpen, openingName, onToggleFavorite, onRender, rendering }) {
  const [query, setQuery] = useState("");
  const [tier, setTier] = useState("all");
  const [element, setElement] = useState("all");
  const [clazz, setClazz] = useState("all");
  const [burst, setBurst] = useState("all");
  const [ownedOnly, setOwnedOnly] = useState(true);
  const [favOnly, setFavOnly] = useState(false);
  const [sort, setSort] = useState("tier");

  const characters = (data && data.characters) || [];
  const counts = useMemo(() => {
    const result = { all: characters.length, T0: 0, T1: 0, T2: 0 };
    characters.forEach((c) => {
      if (result[c.tier] !== undefined) result[c.tier] += 1;
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
        const haystack = [c.name, c.en, c.role, c.core_summary]
          .filter(Boolean)
          .join(" ")
          .toLowerCase();
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
    return (
      <div className="nt-content">
        <div className="nt-stats">
          {[1, 2, 3, 4].map((n) => (
            <div key={n} className="nt-stat nt-skeleton-stat" />
          ))}
        </div>
        <div className="nt-grid">
          {Array.from({ length: 12 }).map((_, index) => (
            <SkeletonCard key={index} />
          ))}
        </div>
      </div>
    );
  }

  if (error && !data) {
    return (
      <div className="nt-content">
        <div className="nt-error-card">
          <div className="nt-error-title">看板加载失败</div>
          <pre className="nt-error-detail">{String(error.message || error)}</pre>
          <button type="button" className="nt-btn" onClick={onRetry}>
            重试
          </button>
        </div>
      </div>
    );
  }

  const owned = counts.all ? data.counts.owned : 0;
  const avg = percent(data.average_score);
  return (
    <div className="nt-content">
      <div className="nt-stats">
        <div className="nt-stat">
          <span className="nt-stat-value">{owned}<small>/{data.counts.targets}</small></span>
          <span className="nt-stat-label">已拥有</span>
        </div>
        <div className="nt-stat">
          <span className="nt-stat-value">{avg === null ? "—" : avg + "%"}</span>
          <span className="nt-stat-label">平均装备成型度</span>
        </div>
        <div className="nt-stat">
          <span className="nt-stat-value">{data.counts.favorites}</span>
          <span className="nt-stat-label">收藏 / 队列</span>
        </div>
        <div className="nt-stat">
          <span className="nt-stat-value">{filtered.length}</span>
          <span className="nt-stat-label">当前显示</span>
        </div>
      </div>

      <Queue characters={characters} onOpen={onOpen} />

      <div className="nt-toolbar">
        <div className="nt-search">
          <span className="nt-search-icon">⌕</span>
          <input
            className="nt-input"
            value={query}
            onChange={(event) => setQuery(event.target.value)}
            placeholder="搜索角色 / 英文名 / 定位"
            aria-label="搜索角色"
          />
        </div>
        <div className="nt-chips" role="group" aria-label="梯度筛选">
          {[
            ["all", "全部", counts.all],
            ["T0", "T0", counts.T0],
            ["T1", "T1", counts.T1],
            ["T2", "T2", counts.T2],
          ].map(([value, label, count]) => (
            <button
              key={value}
              type="button"
              className={"nt-chip" + (tier === value ? " is-active" : "")}
              onClick={() => setTier(value)}
            >
              {label} <span className="nt-chip-count">{count}</span>
            </button>
          ))}
        </div>
        <select className="nt-input nt-select" value={element} onChange={(event) => setElement(event.target.value)} aria-label="属性">
          <option value="all">全部属性</option>
          {["火", "水", "电", "铁", "风"].map((value) => (
            <option key={value} value={value}>{value}</option>
          ))}
        </select>
        <select className="nt-input nt-select" value={clazz} onChange={(event) => setClazz(event.target.value)} aria-label="职业">
          <option value="all">全部职业</option>
          {["火力型", "辅助型", "防御型"].map((value) => (
            <option key={value} value={value}>{value}</option>
          ))}
        </select>
        <select className="nt-input nt-select" value={burst} onChange={(event) => setBurst(event.target.value)} aria-label="爆裂">
          <option value="all">全部爆裂</option>
          {["I", "II", "III"].map((value) => (
            <option key={value} value={value}>爆裂 {value}</option>
          ))}
        </select>
        <select className="nt-input nt-select" value={sort} onChange={(event) => setSort(event.target.value)} aria-label="排序">
          <option value="tier">梯度优先</option>
          <option value="scoreDesc">成型度 高→低</option>
          <option value="scoreAsc">成型度 低→高</option>
          <option value="name">名称</option>
        </select>
        <label className="nt-check">
          <input type="checkbox" checked={!ownedOnly} onChange={(event) => setOwnedOnly(!event.target.checked)} />
          全部图鉴
        </label>
        <label className="nt-check">
          <input type="checkbox" checked={favOnly} onChange={(event) => setFavOnly(event.target.checked)} />
          只看收藏
        </label>
        <button type="button" className="nt-btn nt-btn-ghost" onClick={onRender} disabled={rendering}>
          {rendering ? "渲染中…" : "同步 nikke.md"}
        </button>
      </div>

      {data.warnings && data.warnings.length > 0 ? (
        <div className="nt-warn-strip">
          有 {data.warnings.length} 个进度角色在目标库中找不到，已跳过：{data.warnings.map((w) => w.name).join("、")}
        </div>
      ) : null}

      {filtered.length === 0 ? (
        <div className="nt-empty">
          <div className="nt-empty-title">没有匹配的角色</div>
          <div className="nt-muted">换个筛选条件，或把「全部图鉴」打开。</div>
          <button type="button" className="nt-btn" onClick={resetFilters}>清空筛选</button>
        </div>
      ) : (
        <div className="nt-grid">
          {filtered.map((character) => (
            <CharacterCard
              key={character.name}
              character={character}
              onOpen={onOpen}
              opening={openingName === character.name}
              onToggleFavorite={onToggleFavorite}
            />
          ))}
        </div>
      )}
      <div className="nt-footnote">
        词条百分比的满值上限见 <span className="nt-mono">references/affix_tiers.json</span>；装备强化等级只影响白值，不参与洗条建议。
      </div>
    </div>
  );
}