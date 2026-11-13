import { React, useEffect, useMemo, useRef, useState } from "./sdk.js";
import { affixQuality, nearestTier } from "./util.js";

const SLOT_LABELS = { head: "头部", body: "身躯", arm: "臂部", leg: "腿部" };
const SLOT_KEYS = ["head", "body", "arm", "leg"];

function normalisePiece(piece) {
  const slots = ((piece && piece.slots) || []).slice(0, 3).map((row) => ({
    affix: (row && row.affix) || "",
    value: Number((row && row.value) || 0),
    locked: Boolean(row && row.locked),
  }));
  while (slots.length < 3) slots.push({ affix: "", value: 0, locked: false });
  return {
    level: Math.max(0, Math.min(5, Number((piece && piece.level) || 0))),
    slots,
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
    const label = SLOT_LABELS[slot] + "槽" + (index + 1);
    const meta = affixMeta[row.affix] || {};
    const value = Number(row.value || 0);
    if (!row.affix && value > 0) return label + " 没有词条时数值必须为 0";
    if (row.affix && value <= 0) return label + " 数值必须大于 0";
    if (row.affix && meta.max && value > Number(meta.max) + 0.01) {
      return label + " 超过上限 " + meta.max;
    }
    return "";
  });
}

function TierSelect({ affix, value, meta, onChange, disabled }) {
  const tiers = (meta && meta.tiers) || [];
  if (!tiers.length) {
    return (
      <input
        className="nt-input nt-value-input"
        type="number"
        step="0.01"
        min="0"
        value={value}
        disabled={disabled}
        onChange={(event) => onChange(event.target.value === "" ? 0 : Number(event.target.value))}
        aria-label="词条数值"
      />
    );
  }
  const tier = nearestTier({ [affix]: meta }, affix, value);
  const exact = tier !== null && Math.abs(Number(tiers[tier - 1]) - Number(value)) <= 0.005;
  const current = !value ? "" : exact ? String(tier) : "custom";
  return (
    <select
      className="nt-input nt-tier-select"
      value={current}
      disabled={disabled}
      onChange={(event) => {
        const raw = event.target.value;
        if (raw === "custom" || raw === "") return;
        onChange(Number(tiers[Number(raw) - 1]));
      }}
      aria-label="词条阶数"
    >
      {!value ? <option value="">—</option> : null}
      {tiers.map((tierValue, index) => (
        <option key={index} value={String(index + 1)}>
          {index + 1}阶 · {tierValue}%
        </option>
      ))}
      {value > 0 && !exact ? <option value="custom">自定义 {value}%</option> : null}
    </select>
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
  disabled,
}) {
  return (
    <section className="nt-piece">
      <header className="nt-piece-head">
        <div>
          <h3>{SLOT_LABELS[slot]}</h3>
          <span className="nt-muted">
            Lv{piece.level}
            {score === null || score === undefined ? "" : " · 当前 " + Math.round(score * 100) + "%"}
          </span>
        </div>
        <div className="nt-level">
          <input
            className="nt-input nt-level-input"
            type="number"
            min="0"
            max="5"
            value={piece.level}
            disabled={disabled}
            onChange={(event) => onLevel(Math.max(0, Math.min(5, Number(event.target.value || 0))))}
            aria-label="装备强化等级"
          />
        </div>
      </header>

      <div className="nt-affix-list">
        {piece.slots.map((row, index) => {
          const meta = affixMeta[row.affix] || {};
          const quality = affixQuality(affixMeta, row.affix, row.value);
          const isMain = mainRecommended.has(row.affix);
          const isSub = subRecommended.has(row.affix);
          return (
            <div className="nt-affix-row" key={index}>
              <div className="nt-affix-kind">
                <select
                  className="nt-input nt-affix-select"
                  value={row.affix || ""}
                  disabled={disabled}
                  onChange={(event) => onAffix(index, event.target.value)}
                  aria-label={"槽" + (index + 1) + " 词条类型"}
                >
                  <option value="">无词条</option>
                  {affixOptions.map((affix) => (
                    <option key={affix} value={affix}>
                      {affix}
                    </option>
                  ))}
                </select>
              </div>
              <div className={"nt-value-wrap" + (row.affix ? "" : " is-disabled")}>
                <TierSelect
                  affix={row.affix}
                  value={row.value}
                  meta={meta}
                  disabled={disabled || !row.affix}
                  onChange={(value) => onRow(index, { value })}
                />
              </div>
              <button
                type="button"
                className={"nt-lock" + (row.locked ? " is-on" : "")}
                onClick={() => onRow(index, { locked: !row.locked })}
                disabled={disabled}
                title={row.locked ? "已锁定，点击解锁" : "未锁定，点击锁定"}
                aria-label={row.locked ? "解锁" : "锁定"}
              >
                {row.locked ? "🔒" : "🔓"}
              </button>
              <div className="nt-affix-live">
                {row.affix ? (
                  <>
                    <span className="nt-quality-track">
                      <span
                        style={{
                          width: Math.round(quality * 100) + "%",
                          background: quality >= 0.8 ? "#5fd08a" : quality >= 0.55 ? "#f2c14e" : "#ff7a6b",
                        }}
                      />
                    </span>
                    {isMain ? <span className="nt-reco">主</span> : null}
                    {isSub ? <span className="nt-reco nt-reco-sub">副</span> : null}
                  </>
                ) : (
                  <span className="nt-muted">空槽</span>
                )}
              </div>
            </div>
          );
        })}
      </div>

      {error ? <div className="nt-inline-error">{error}</div> : null}
    </section>
  );
}

export function GearBoard({ gear, diag, target, affixMeta, affixOptions, serverRev, onSave, disabled }) {
  const [drafts, setDrafts] = useState(() => normaliseGear(gear));
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");
  const baseline = useMemo(() => normaliseGear(gear), [JSON.stringify(gear || {})]);
  const dirtyRef = useRef(false);

  const scores = useMemo(() => {
    const out = {};
    ((diag && diag.pieces) || []).forEach((piece) => {
      out[piece.slot] = piece.score;
    });
    return out;
  }, [diag]);

  const mainRecommended = useMemo(
    () =>
      new Set(
        (((target && target.requirement && target.requirement.affixes) || [])).map((item) => item.affix)
      ),
    [target]
  );
  const subRecommended = useMemo(
    () =>
      new Set(
        (((target && target.requirement && target.requirement.sub_affixes) || [])).map((item) => item.affix)
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
          slots: piece.slots.map((row, rowIndex) =>
            rowIndex === index ? { ...row, ...patch } : row
          ),
        },
      };
    });
  };

  const changeAffix = (slot, index, affix) => {
    const tiers = (affixMeta[affix] && affixMeta[affix].tiers) || [];
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

  return (
    <div className="nt-gear">
      <div className="nt-gear-bar">
        <div className="nt-gear-state">
          <strong>装备词条</strong>
          <span className="nt-muted">
            {dirty ? dirtySlots.length + " 件未保存" : "已保存"}
          </span>
        </div>
        <div className="nt-gear-actions">
          <button
            type="button"
            className="nt-btn nt-btn-ghost"
            onClick={reset}
            disabled={!dirty || saving}
          >
            撤销
          </button>
          <button
            type="button"
            className="nt-btn"
            onClick={submit}
            disabled={!dirty || Boolean(errors.first) || saving || disabled}
          >
            {saving ? "保存中…" : "保存四件"}
          </button>
        </div>
      </div>
      {error ? <div className="nt-inline-error">{error}</div> : null}
      <div className="nt-pieces">
        {SLOT_KEYS.map((slot) => (
          <PieceEditor
            key={slot}
            slot={slot}
            piece={drafts[slot]}
            score={scores[slot]}
            error={errors.bySlot[slot].find(Boolean) || ""}
            mainRecommended={mainRecommended}
            subRecommended={subRecommended}
            affixMeta={affixMeta}
            affixOptions={affixOptions}
            onLevel={(level) => patchPiece(slot, { level })}
            onRow={(index, patch) => patchRow(slot, index, patch)}
            onAffix={(index, affix) => changeAffix(slot, index, affix)}
            disabled={disabled || saving}
          />
        ))}
      </div>
    </div>
  );
}
