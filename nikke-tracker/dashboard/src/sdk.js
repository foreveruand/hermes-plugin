// Access the host SDK without bundling React.
const SDK = window.__HERMES_PLUGIN_SDK__;
if (!SDK) {
  throw new Error("Hermes Plugin SDK not found (window.__HERMES_PLUGIN_SDK__)");
}
const React = SDK.React;
const hooks = SDK.hooks || {};
const useState = hooks.useState;
const useEffect = hooks.useEffect;
const useMemo = hooks.useMemo;
const useRef = hooks.useRef;
const useCallback = hooks.useCallback;
const cn = (SDK.utils && SDK.utils.cn) || ((...parts) => parts.filter(Boolean).join(" "));
const fetchJSON = SDK.fetchJSON;
const authedFetch = SDK.authedFetch;

export {
  SDK,
  React,
  useState,
  useEffect,
  useMemo,
  useRef,
  useCallback,
  cn,
  fetchJSON,
  authedFetch,
};
