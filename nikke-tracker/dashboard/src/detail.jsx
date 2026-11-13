import {
  React,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "./sdk.js";
import { Portrait } from "./portrait.jsx";
import { percent, scoreColor, cleanAdvice } from "./util.js";
import { GearBoard } from "./gear.jsx";
import { getDetail, saveGearBatch, saveMeta, removeCharacter, saveTarget, uploadPortrait } from "./api.js";

function BigRing({ score, grade }) {
  const pct = percent(score);
  if (pct === null) return null;
  const size = 116;
  const stroke = 8;
  const radius = (size - stroke) / 2;
  const circumference = 2 * Math.PI * radius;
  const filled = Math.max(0, Math.min(1, Number(score))) * circumference;
  return (
    <div className="nt-big-ring" title="按四部位装备词条计算">
      <svg width={size} height={size} viewBox={"0 0 " + size + " " + size}>
        <circle cx={size / 2} cy={size / 2} r={radius} fill="none" stroke="rgba(255,255,255,0.14)" strokeWidth={stroke} />
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
      <div className="nt-big-ring-text">
        <strong>{pct}%</strong>
        <span>{grade || "—"}</span>
      </div>
    </div>
  );
}

function TierBadge({ tier }) {
  if (!tier) return null;
  return <span className={"nt-tier nt-tier-" + tier}>{tier}</span>;
}

function pieceScore(piece) {
  return Math.round((piece && piece.score ? piece.score : 0) * 100);
}

const DETAIL_CACHE = new Map();

export function storeDetail(payload) {
  if (payload && payload.character && payload.character.name) {
    DETAIL_CACHE.set(payload.character.name, payload);
  }
}

export function clearDetailCache() {
  DETAIL_CACHE.clear();
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
    notes: character.notes || "",
  }));
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");

  useEffect(() => {
    setDraft({
      skills: (character.skills || [1, 1, 1]).slice(0, 3),
      collectible: character.collectible || "",
      notes: character.notes || "",
    });
    setError("");
  }, [JSON.stringify(character.skills), character.collectible, character.notes]);

  const dirty =
    JSON.stringify(draft.skills) !== JSON.stringify((character.skills || [1, 1, 1]).slice(0, 3)) ||
    draft.collectible !== (character.collectible || "") ||
    draft.notes !== (character.notes || "");

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

  return (
    <section className="nt-panel nt-meta">
      <div className="nt-panel-head">
        <h3>技能 / 收藏品</h3>
        <span className="nt-muted">目标技能 {(target.skills || []).join("/")}</span>
      </div>
      <div className="nt-meta-grid">
        <label className="nt-field">
          <span>技能 1</span>
          <input className="nt-input" type="number" min="1" max="10" value={draft.skills[0]} onChange={(event) => setSkill(0, event.target.value)} />
        </label>
        <label className="nt-field">
          <span>技能 2</span>
          <input className="nt-input" type="number" min="1" max="10" value={draft.skills[1]} onChange={(event) => setSkill(1, event.target.value)} />
        </label>
        <label className="nt-field">
          <span>爆裂</span>
          <input className="nt-input" type="number" min="1" max="10" value={draft.skills[2]} onChange={(event) => setSkill(2, event.target.value)} />
        </label>
        <label className="nt-field">
          <span>收藏品</span>
          <input className="nt-input" value={draft.collectible} onChange={(event) => setDraft({ ...draft, collectible: event.target.value })} placeholder={target.collectible || "未记录"} />
        </label>
      </div>
      <label className="nt-field nt-field-wide">
        <span>备注</span>
        <textarea className="nt-input nt-textarea" value={draft.notes} onChange={(event) => setDraft({ ...draft, notes: event.target.value })} rows={2} placeholder="例如：先不冲 12 词条" />
      </label>
      {error ? <div className="nt-inline-error">{error}</div> : null}
      <div className="nt-panel-foot">
        <span className="nt-muted">{dirty ? "有未保存修改" : "已保存"}</span>
        <button type="button" className="nt-btn" onClick={submit} disabled={!dirty || saving || disabled}>
          {saving ? "保存中…" : "保存信息"}
        </button>
      </div>
    </section>
  );
}

function AdvicePanel({ diag }) {
  if (!diag) return null;
  const total = (diag.pieces || []).reduce((sum, piece) => sum + (piece.remaining_cost || 0), 0);
  return (
    <section className="nt-panel nt-advice">
      <div className="nt-panel-head">
        <h3>下一步建议</h3>
        <span className="nt-muted">预计合计 {total.toFixed(1)} 石头（按每件独立估算）</span>
      </div>
      <div className="nt-actions-list">
        {(diag.actions || []).map((action, index) => (
          <div className="nt-action" key={index}>
            <span className="nt-action-index">{index + 1}</span>
            <span>{cleanAdvice(action)}</span>
          </div>
        ))}
      </div>
      <div className="nt-piece-advice">
        {(diag.pieces || []).map((piece) => (
          <details className="nt-advice-piece" key={piece.slot}>
            <summary>
              <span>{piece.label}</span>
              <span className="nt-advice-score">{pieceScore(piece)}%</span>
              {piece.bad && piece.bad.length ? <span className="nt-advice-gap">缺口 {piece.bad.length}</span> : null}
              {piece.remaining_cost ? <span className="nt-advice-cost">{piece.remaining_cost} 石头</span> : null}
            </summary>
            {(piece.issues || []).map((issue, index) => (
              <div key={"i" + index} className={"nt-issue nt-issue-" + issue.severity}>
                {issue.text}
              </div>
            ))}
            {(piece.steps || []).map((step, index) => (
              <div key={"s" + index} className="nt-step">· {step}</div>
            ))}
            {!(piece.issues || []).length && !(piece.steps || []).length ? <div className="nt-muted">暂无问题</div> : null}
          </details>
        ))}
      </div>
    </section>
  );
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
    setMain((previous) =>
      previous.includes(affix) ? previous.filter((item) => item !== affix) : [...previous, affix]
    );
    setSub((previous) => previous.filter((item) => item !== affix));
  };

  const toggleSub = (affix) => {
    if (main.includes(affix)) return;
    setSub((previous) =>
      previous.includes(affix) ? previous.filter((item) => item !== affix) : [...previous, affix]
    );
  };

  return (
    <section className="nt-panel nt-target-editor">
      <div className="nt-panel-head">
        <h3>校核推荐养成</h3>
        <span className="nt-muted">保存后卡片成型度会按新目标重算</span>
      </div>
      <div className="nt-edit-grid">
        <label className="nt-field">
          <span>梯度</span>
          <select className="nt-input" value={tier} onChange={(event) => setTier(event.target.value)}>
            {["T0", "T1", "T2"].map((value) => <option key={value} value={value}>{value}</option>)}
          </select>
        </label>
        {["技能1", "技能2", "爆裂"].map((label, index) => (
          <label className="nt-field" key={label}>
            <span>{label}</span>
            <input
              className="nt-input"
              type="number"
              min="1"
              max="10"
              value={skills[index]}
              onChange={(event) => setSkill(index, event.target.value)}
            />
          </label>
        ))}
      </div>

      <div className="nt-edit-section">
        <div className="nt-edit-label">主词条（优先刷）</div>
        <div className="nt-pick-list">
          {affixOptions.map((affix) => (
            <button
              key={affix}
              type="button"
              className={"nt-pick" + (main.includes(affix) ? " is-main" : "")}
              onClick={() => toggleMain(affix)}
            >
              {affix}
            </button>
          ))}
        </div>
      </div>

      <div className="nt-edit-section">
        <div className="nt-edit-label">副词条（有更好）</div>
        <div className="nt-pick-list">
          {affixOptions.map((affix) => (
            <button
              key={affix}
              type="button"
              className={"nt-pick" + (sub.includes(affix) ? " is-sub" : "")}
              disabled={main.includes(affix)}
              onClick={() => toggleSub(affix)}
            >
              {affix}
            </button>
          ))}
        </div>
      </div>

      <label className="nt-field nt-field-wide">
        <span>校核说明</span>
        <input
          className="nt-input"
          value={affixNote}
          onChange={(event) => setAffixNote(event.target.value)}
          placeholder="例如：主: 装弹1+ | 副: 优越/攻击"
        />
      </label>
      <label className="nt-field nt-field-wide">
        <span>来源 / 备注</span>
        <textarea
          className="nt-input nt-textarea"
          rows={3}
          value={note}
          onChange={(event) => setNote(event.target.value)}
        />
      </label>
      <div className="nt-panel-foot">
        <button type="button" className="nt-btn nt-btn-ghost" onClick={onCancel} disabled={saving}>取消</button>
        <button
          type="button"
          className="nt-btn"
          disabled={saving}
          onClick={() => onSave({
            tier,
            skills,
            affix_main: main,
            affix_sub: sub,
            affix_note: affixNote,
            note,
          })}
        >
          {saving ? "保存中…" : "保存校核"}
        </button>
      </div>
    </section>
  );
}

function TargetSummary({ target, onEdit, inline }) {
  const requirement = (target && target.requirement) || {};
  const main = requirement.affixes || [];
  const sub = requirement.sub_affixes || [];
  const mainText = requirement.no_requirement
    ? "无特殊要求"
    : main.length
      ? main.map((item) => item.text || item.affix).join(" / ")
      : requirement.known
        ? "—"
        : "未校核";
  const subText = sub.length ? sub.map((item) => item.text || item.affix).join(" / ") : "—";
  return (
    <div className={"nt-target-box" + (inline ? " is-inline" : "")}>
      <div className="nt-target-head">
        <span className="nt-target-title">目标基准</span>
        {target && target.guide_date ? <span className="nt-muted">一图流 {target.guide_date}</span> : null}
        <button type="button" className="nt-btn nt-btn-ghost" onClick={onEdit}>校核</button>
      </div>
      <div className="nt-target-row">
        <span>核心词条</span>
        <strong className={requirement.no_requirement ? "nt-req-none" : ""}>{mainText}</strong>
      </div>
      <div className="nt-target-row">
        <span>副词条</span>
        <strong>{subText}</strong>
      </div>
      <div className="nt-target-row">
        <span>魔方 / 收藏品</span>
        <strong>{(target && target.cube) || "—"} / {(target && target.collectible) || "—"}</strong>
      </div>
      {target && target.affix_note ? (
        <div className="nt-target-note" title={target.affix_note}>{target.affix_note}</div>
      ) : null}
    </div>
  );
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

  return (
    <div
      className="nt-modal-overlay"
      onMouseDown={(event) => {
        if (event.target === event.currentTarget) onClose();
      }}
    >
      <div className="nt-modal" role="dialog" aria-modal="true" aria-label={title || "角色详情"}>
        <div className="nt-modal-bar">
          <div className="nt-modal-title">{title || "角色详情"}</div>
          <div className="nt-modal-actions">
            {actions}
            <button type="button" className="nt-modal-close" onClick={onClose} aria-label="关闭">×</button>
          </div>
        </div>
        <div className="nt-modal-body">{children}</div>
      </div>
    </div>
  );
}

export function Detail({ name, bootstrap, onClose, onSaved, onAdd, onFetchPortrait, onTargetSaved, onPortraitUploaded, onToggleFavorite, toast }) {
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
    getDetail(name)
      .then((next) => {
        setDetail(next);
        setConflict(false);
      })
      .catch((err) => setError(err))
      .finally(() => setLoading(false));
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
    const list = (bootstrap && bootstrap.characters) || [];
    const canonical = (detail && detail.character && detail.character.name) || name;
    return list.find((item) => item.name === canonical) || null;
  }, [bootstrap, name, detail]);

  if (loading && !detail) {
    return (
      <DetailModal title={name} onClose={onClose}>
        <div className="nt-detail-skeleton">
          <div className="nt-skeleton-art" />
          <div className="nt-skeleton-line" />
          <div className="nt-skeleton-line short" />
        </div>
      </DetailModal>
    );
  }

  if (error && !detail) {
    return (
      <DetailModal title={name} onClose={onClose}>
        <div className="nt-error-card">
          <div className="nt-error-title">详情加载失败</div>
          <pre className="nt-error-detail">{String(error.message || error)}</pre>
          <button type="button" className="nt-btn" onClick={load}>重试</button>
        </div>
      </DetailModal>
    );
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
        favorite: payload.favorite,
      }));
      if (onSaved) onSaved(payload);
      const render = payload.render;
      if (render && render.ok) {
        toast(render.changed ? "已保存并同步 nikke.md" : "已保存（Markdown 无变化）", "ok");
      } else {
        toast("已保存，但渲染失败：" + ((render && render.error) || "未知错误"), "warn");
      }
    } catch (err) {
      if (err.status === 409) {
        setConflict(true);
        toast("数据已被其他页面更新，请先刷新详情", "warn");
      } else {
        toast(err.message || "保存失败", "error");
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
        favorite: payload.favorite,
      }));
      if (onSaved) onSaved(payload);
      const render = payload.render;
      toast(render && render.ok ? "已保存并同步 nikke.md" : "已保存，但渲染失败", render && render.ok ? "ok" : "warn");
    } catch (err) {
      if (err.status === 409) {
        setConflict(true);
        toast("数据已被其他页面更新，请先刷新详情", "warn");
      } else {
        toast(err.message || "保存失败", "error");
      }
      throw err;
    }
  };

  const handleRemove = async () => {
    if (!window.confirm("确定把「" + character.name + "」移出看板？进度数据会被删除。")) return;
    setRemoving(true);
    try {
      await removeCharacter(character.name, detail.rev);
      toast("已移出看板", "ok");
      onClose(true);
    } catch (err) {
      toast(err.message || "移出失败", "error");
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
      toast(err.message || "加入失败", "error");
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
        targets_rev: detail.targets_rev,
      });
      setDetail((previous) => ({
        ...previous,
        rev: payload.rev,
        targets_rev: payload.targets_rev,
        character: payload.character,
        target: payload.target,
        diag: payload.diag,
        owned: payload.owned,
        favorite: payload.favorite,
      }));
      setEditingTarget(false);
      if (onTargetSaved) onTargetSaved(payload);
      const render = payload.render;
      toast(
        render && render.ok ? "推荐信息已校核并同步 nikke.md" : "推荐信息已保存，但 Markdown 渲染失败",
        render && render.ok ? "ok" : "warn"
      );
    } catch (err) {
      toast(err.message || "校核保存失败", "error");
    } finally {
      setSavingTarget(false);
    }
  };

  const handlePortraitFile = (event) => {
    const file = event.target.files && event.target.files[0];
    event.target.value = "";
    if (!file) return;
    if (file.size > 8 * 1024 * 1024) {
      toast("图片不能超过 8MB", "warn");
      return;
    }
    const reader = new FileReader();
    reader.onload = async () => {
      setUploadingPortrait(true);
      try {
        const result = await uploadPortrait(character.name, String(reader.result || ""));
        toast("头像已更新", "ok");
        if (onPortraitUploaded) await onPortraitUploaded(result);
      } catch (err) {
        toast(err.message || "头像上传失败", "error");
      } finally {
        setUploadingPortrait(false);
      }
    };
    reader.readAsDataURL(file);
  };

  return (
    <DetailModal
      title={character.name}
      onClose={onClose}
      actions={
        <>
          <button
            type="button"
            className={"nt-btn nt-btn-ghost" + (isFavorite ? " is-fav" : "")}
            onClick={handleToggleFavorite}
          >
            {isFavorite ? "★ 已收藏" : "☆ 加入队列"}
          </button>
          {owned ? (
            <button type="button" className="nt-btn nt-btn-danger" onClick={handleRemove} disabled={removing}>
              {removing ? "移出中…" : "移出看板"}
            </button>
          ) : null}
          {!portrait || !portrait.has_cache ? (
            <button type="button" className="nt-btn nt-btn-ghost" onClick={handleFetchPortrait} disabled={fetchingPortrait}>
              {fetchingPortrait ? "抓取中…" : "补头像"}
            </button>
          ) : null}
          <button
            type="button"
            className="nt-btn nt-btn-ghost"
            onClick={() => fileInputRef.current && fileInputRef.current.click()}
            disabled={uploadingPortrait}
          >
            {uploadingPortrait ? "上传中…" : "上传头像"}
          </button>
          <input
            ref={fileInputRef}
            type="file"
            accept="image/png,image/jpeg,image/webp"
            className="nt-file-hidden"
            onChange={handlePortraitFile}
          />
        </>
      }
    >
      <section className="nt-hero">
        <div className="nt-hero-art">
          <Portrait name={character.name} element={character.element} portrait={portrait} className="nt-hero-portrait" />
        </div>
        <div className="nt-hero-info">
          <div className="nt-hero-title">
            <TierBadge tier={character.tier} />
            <h2>{character.name}</h2>
            <span className="nt-muted">{character.element} / {character.class} · 爆裂 {character.burst} · {character.weapon}</span>
          </div>
          <div className="nt-role">{character.role || "—"}</div>
          {owned ? (
            <div className="nt-hero-stats">
              <div className="nt-hero-score">
                <BigRing score={diag ? diag.score : 0} grade={diag ? diag.grade : ""} />
              </div>
              <div className="nt-hero-facts">
                <div><span>技能</span><strong>{(character.skills || []).join("/")}</strong><em>目标 {(target.skills || []).join("/")}</em></div>
                <div>
                  <span>有效词条</span>
                  <strong>{diag ? diag.effective : 0} 条</strong>
                  <em>按当前装备统计</em>
                </div>
                <div><span>魔方</span><strong>{target.cube || "—"}</strong><em>收藏品 {character.collectible || "—"}</em></div>
                <div><span>下一动作</span><strong className="nt-priority">{diag ? diag.priority : "—"}</strong></div>
              </div>
            </div>
          ) : (
            <div className="nt-add-box">
              <p>这个角色还没有加入看板。点卡片时会自动加入；直接打开详情时也可以在这里加入。加入后以技能 1/1/1、空装备开始，不会自动填目标等级。</p>
              <button type="button" className="nt-btn" onClick={handleAdd} disabled={adding}>
                {adding ? "加入中…" : "加入看板"}
              </button>
            </div>
          )}
          <TargetSummary target={target} inline onEdit={() => setEditingTarget(true)} />
        </div>
      </section>

      {conflict ? (
        <div className="nt-conflict">
          <span>数据已被其他页面更新。刷新会放弃你当前未保存的修改。</span>
          <button type="button" className="nt-btn nt-btn-ghost" onClick={load}>刷新详情</button>
        </div>
      ) : null}

      {editingTarget ? (
        <div id="nt-target-editor">
          <TargetEditor
            target={target}
            affixOptions={(detail.affix_options) || (bootstrap && bootstrap.affix_options) || []}
            onSave={handleSaveTarget}
            onCancel={() => setEditingTarget(false)}
            saving={savingTarget}
          />
        </div>
      ) : null}

      {owned ? (
        <div className="nt-detail-grid">
          <GearBoard
            key={character.name}
            gear={character.gear}
            diag={diag}
            target={target}
            affixMeta={(detail.affix_meta) || (bootstrap && bootstrap.affix_meta) || {}}
            affixOptions={(detail.affix_options) || (bootstrap && bootstrap.affix_options) || []}
            serverRev={detail.rev}
            onSave={handleSaveGear}
            disabled={Boolean(conflict) || !canEdit}
          />
          <div className="nt-side">
            <AdvicePanel diag={diag} />
            <MetaEditor character={character} target={target} onSave={handleSaveMeta} disabled={Boolean(conflict) || !canEdit} />
          </div>
        </div>
      ) : null}
    </DetailModal>
  );
}