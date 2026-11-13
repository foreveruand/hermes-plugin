import { React, useState } from "./sdk.js";

function Block({ block }) {
  if (block.type === "heading") {
    const Tag = "h" + Math.min(6, Math.max(2, block.level || 2));
    return <Tag className={"nt-md-h nt-md-h" + (block.level || 2)}>{block.text}</Tag>;
  }
  if (block.type === "paragraph") {
    return <p className="nt-md-p">{block.text}</p>;
  }
  if (block.type === "list") {
    return (
      <ul className="nt-md-list">
        {(block.items || []).map((item, index) => <li key={index}>{item}</li>)}
      </ul>
    );
  }
  if (block.type === "table") {
    return (
      <div className="nt-md-table-wrap">
        <table className="nt-md-table">
          <thead>
            <tr>{(block.headers || []).map((cell, index) => <th key={index}>{cell}</th>)}</tr>
          </thead>
          <tbody>
            {(block.rows || []).map((row, rowIndex) => (
              <tr key={rowIndex}>
                {row.map((cell, cellIndex) => <td key={cellIndex}>{cell}</td>)}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    );
  }
  return null;
}

function TeamDoc({ doc, tone }) {
  if (!doc) return null;
  return (
    <article className={"nt-team-doc nt-team-" + tone}>
      {tone === "solo" ? (
        <div className="nt-team-banner">
          单人突击每赛季换 Boss，属性和弱点必须按当季公告核对；本页是当前技能里的赛季记录，不保证仍然有效。
        </div>
      ) : null}
      {(doc.blocks || []).length ? (
        (doc.blocks || []).map((block, index) => <Block key={index} block={block} />)
      ) : (
        <pre className="nt-raw">{doc.raw || "暂无内容"}</pre>
      )}
      <footer className="nt-team-foot">
        来源 <span className="nt-mono">{doc.source}</span>
        {doc.updated ? <span> · 文件更新 {doc.updated.slice(0, 16).replace("T", " ")}</span> : null}
      </footer>
    </article>
  );
}

export function Teams({ data, loading, error, onRetry }) {
  const [tab, setTab] = useState("interception");

  if (loading && !data) {
    return (
      <div className="nt-content">
        <div className="nt-detail-skeleton">
          <div className="nt-skeleton-line" />
          <div className="nt-skeleton-line short" />
          <div className="nt-skeleton-line" />
        </div>
      </div>
    );
  }
  if (error && !data) {
    return (
      <div className="nt-content">
        <div className="nt-error-card">
          <div className="nt-error-title">配队参考加载失败</div>
          <pre className="nt-error-detail">{String(error.message || error)}</pre>
          <button type="button" className="nt-btn" onClick={onRetry}>重试</button>
        </div>
      </div>
    );
  }

  return (
    <div className="nt-content nt-teams">
      <div className="nt-team-tabs" role="tablist">
        <button
          type="button"
          role="tab"
          aria-selected={tab === "interception"}
          className={"nt-team-tab" + (tab === "interception" ? " is-active" : "")}
          onClick={() => setTab("interception")}
        >
          异常拦截
        </button>
        <button
          type="button"
          role="tab"
          aria-selected={tab === "solo"}
          className={"nt-team-tab" + (tab === "solo" ? " is-active" : "")}
          onClick={() => setTab("solo")}
        >
          单人突击
        </button>
      </div>
      {tab === "interception" ? (
        <TeamDoc doc={data && data.interception} tone="interception" />
      ) : (
        <TeamDoc doc={data && data.solo_raid} tone="solo" />
      )}
    </div>
  );
}
