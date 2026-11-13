import { fetchJSON, authedFetch } from "./sdk.js";

const API = "/api/plugins/nikke-tracker";

async function call(path, init) {
  try {
    return await fetchJSON(API + path, init);
  } catch (err) {
    const error = new Error((err && err.message) || "请求失败");
    error.status = err && err.status;
    error.body = err && err.body;
    throw error;
  }
}

export const getBootstrap = () => call("/bootstrap");
export const getDetail = (name) =>
  call("/detail?name=" + encodeURIComponent(name));
export const getTeams = () => call("/teams");
export const renderNote = () => call("/render", { method: "POST" });

export const saveGear = (body) =>
  call("/gear", {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });

export const saveGearBatch = (body) =>
  call("/gear/batch", {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });

export const saveMeta = (body) =>
  call("/meta", {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });

export const saveTarget = (body) =>
  call("/target", {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });

export const uploadPortrait = (name, dataUrl) =>
  call("/portrait/upload", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ name, data_url: dataUrl }),
  });

export const addCharacter = (name) =>
  call("/character", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ name }),
  });

export const removeCharacter = (name, rev) =>
  call(
    "/character/" +
      encodeURIComponent(name) +
      "?confirm=1" +
      (rev ? "&rev=" + encodeURIComponent(rev) : ""),
    { method: "DELETE" }
  );

export const fetchPortrait = (name) =>
  call("/portrait/fetch", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ name }),
  });

export const setFavorites = (favorites) =>
  call("/state", {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ favorites }),
  });

export async function getPortraitBytes(key, version) {
  const suffix = version ? "?v=" + encodeURIComponent(version) : "";
  const res = await authedFetch(API + "/portrait/" + encodeURIComponent(key) + suffix);
  if (!res.ok) throw new Error("头像不可用");
  return res.blob();
}