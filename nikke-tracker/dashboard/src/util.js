export const TIER_ORDER = { T0: 0, T1: 1, T2: 2 };

export const ELEMENT_COLORS = {
  火: "#ff6b4a",
  水: "#4aa8ff",
  电: "#c77dff",
  铁: "#c9c9c9",
  风: "#5fd08a",
};

export function elementColor(element) {
  return ELEMENT_COLORS[element] || "#8a8f98";
}

export function tierRank(tier) {
  return tier in TIER_ORDER ? TIER_ORDER[tier] : 9;
}

export function gradeColor(grade) {
  if (grade === "毕业" || grade === "达标") return "#5fd08a";
  if (grade === "成型") return "#64d6c0";
  if (grade === "过渡") return "#f2c14e";
  return "#9aa0a6";
}

export function percent(score) {
  if (score === null || score === undefined) return null;
  return Math.round(Number(score) * 100);
}

export function scoreColor(score) {
  if (score === null || score === undefined) return "#9aa0a6";
  if (score >= 0.85) return "#5fd08a";
  if (score >= 0.68) return "#64d6c0";
  if (score >= 0.5) return "#f2c14e";
  return "#9aa0a6";
}

export function sortCharacters(list, mode) {
  const copy = [...list];
  const byName = (a, b) => a.name.localeCompare(b.name, "zh-Hans-CN");
  if (mode === "name") return copy.sort(byName);
  if (mode === "scoreAsc") {
    return copy.sort(
      (a, b) =>
        (a.score === null ? 2 : 0) - (b.score === null ? 2 : 0) ||
        (a.score ?? 0) - (b.score ?? 0) ||
        byName(a, b)
    );
  }
  if (mode === "scoreDesc") {
    return copy.sort(
      (a, b) =>
        (a.score === null ? 2 : 0) - (b.score === null ? 2 : 0) ||
        (b.score ?? 0) - (a.score ?? 0) ||
        byName(a, b)
    );
  }
  return copy.sort(
    (a, b) =>
      tierRank(a.tier) - tierRank(b.tier) ||
      (b.score ?? -1) - (a.score ?? -1) ||
      byName(a, b)
  );
}

export function favoriteQueue(list) {
  return list
    .filter((c) => c.favorite && c.owned)
    .sort(
      (a, b) =>
        tierRank(a.tier) - tierRank(b.tier) ||
        (a.score ?? 1) - (b.score ?? 1) ||
        a.name.localeCompare(b.name, "zh-Hans-CN")
    );
}

export function affixQuality(meta, affix, value) {
  const max = meta && meta[affix] && meta[affix].max;
  if (!max || !value) return 0;
  return Math.max(0, Math.min(1, Number(value) / Number(max)));
}

export function nearestTier(meta, affix, value) {
  const tiers = (meta && meta[affix] && meta[affix].tiers) || [];
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

export function tierLabel(tier) {
  if (!tier) return "";
  if (tier >= 13) return tier + "阶·黑底";
  if (tier >= 10) return tier + "阶·蓝标";
  return tier + "阶";
}

export function summaryFromPayload(payload) {
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
    favorite: Boolean(payload.favorite),
  };
}

export function cleanAdvice(text) {
  if (!text) return "";
  return String(text).replace(/有效词条\s*(\d+)\/\d+/g, "有效词条 $1");
}

export function sameSlots(a, b) {
  return JSON.stringify(a) === JSON.stringify(b);
}
