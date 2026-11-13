import { React, useEffect, useState } from "./sdk.js";
import { getPortraitBytes } from "./api.js";
import { elementColor } from "./util.js";

const cache = new Map();
const inflight = new Map();

export function Portrait({ name, element, portrait, className }) {
  const key = portrait && portrait.key;
  const version = (portrait && portrait.version) || 0;
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
      return undefined;
    }
    const hit = cache.get(cacheKey);
    if (hit && hit.url) {
      setUrl(hit.url);
      return undefined;
    }
    // Wall and detail can mount the same portrait at once; share one request.
    let pending = inflight.get(cacheKey);
    if (!pending) {
      pending = getPortraitBytes(key, version)
        .then((blob) => {
          const objectUrl = URL.createObjectURL(blob);
          cache.set(cacheKey, { url: objectUrl });
          return objectUrl;
        })
        .finally(() => inflight.delete(cacheKey));
      inflight.set(cacheKey, pending);
    }
    pending
      .then((objectUrl) => {
        if (alive) setUrl(objectUrl);
      })
      .catch(() => {
        if (alive) setUrl(null);
      });
    return () => {
      alive = false;
    };
  }, [key, hasCache, version, cacheKey]);

  if (url) {
    return (
      <img
        className={className || "nt-portrait-img"}
        src={url}
        alt={name}
        loading="lazy"
        decoding="async"
        draggable={false}
      />
    );
  }
  const color = elementColor(element);
  return (
    <div
      className={"nt-portrait-fallback " + (className || "")}
      style={{
        background:
          "radial-gradient(circle at 50% 28%, " +
          color +
          "66 0%, " +
          color +
          "22 45%, #0b0f14 100%)",
      }}
    >
      <span className="nt-portrait-initial">{String(name || "?").slice(0, 1)}</span>
    </div>
  );
}