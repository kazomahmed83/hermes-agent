/**
 * One AI Employee — brand lockup + cockpit + approvals desk.
 *
 * Hand-written IIFE against window.__HERMES_PLUGIN_SDK__ (no build step).
 * Data comes from this plugin's own backend at /api/plugins/one-ai-employee/.
 *
 * The approvals desk is the point: Ahmed decides here, in his own OS, and the
 * decision is written to the swarm's Kanban card — durable, auditable, and
 * visible from the CLI and the board. Nothing here touches GoHighLevel.
 */
(function () {
  "use strict";

  var SDK = window.__HERMES_PLUGIN_SDK__;
  if (!SDK) return;
  var React = SDK.React;
  var h = React.createElement;
  var useState = SDK.hooks.useState;
  var useEffect = SDK.hooks.useEffect;
  var useCallback = SDK.hooks.useCallback;
  var C = SDK.components;

  var API = "/api/plugins/one-ai-employee";

  var SERIF = "Constantia, Georgia, 'Times New Roman', serif";
  var MONO = "'Cascadia Code', Consolas, ui-monospace, monospace";
  var GREEN = "var(--color-success, #2FBF71)";
  var WARN = "var(--color-warning, #B7791F)";
  // Max's identity blue, fixed rather than themed: starting work is the blue
  // action, approving it is the green one. Same two colours as the brand.
  var BLUE = "#4C9BE8";
  var LINE = "var(--color-border, #e5decd)";

  /* ---------------------------------------------------------------
   * Agent identities.
   *
   * Ahmed named Adam and Max; the specialists are known by their craft, so
   * they are labelled by role rather than a name he never chose. Colours are
   * fixed hexes (not theme vars) because an agent's identity should stay the
   * same colour whichever theme is active — that is the point of an identity.
   * ------------------------------------------------------------- */
  var AGENTS = {
    "default":   { name: "Adam",                role: "Chief of Staff",       color: "#3DDC84", initial: "A" },
    "ghl":       { name: "Max",                 role: "GoHighLevel Builder",  color: "#4C9BE8", initial: "M" },
    "marketing": { name: "Marketing Strategist", role: "Strategy & Offer",    color: "#A78BFA", initial: "S" },
    "creative":  { name: "Creative Director",   role: "Copy & Creative",      color: "#F2765C", initial: "C" },
  };
  var FALLBACK_COLORS = ["#E0A83D", "#5FC9C0", "#E86A9B", "#8FA1FF"];

  function agentOf(profile) {
    if (AGENTS[profile]) return AGENTS[profile];
    var idx = 0;
    for (var i = 0; i < String(profile).length; i++) idx += String(profile).charCodeAt(i);
    return {
      name: profile,
      role: "",
      color: FALLBACK_COLORS[idx % FALLBACK_COLORS.length],
      initial: String(profile).charAt(0).toUpperCase() || "?",
    };
  }

  /* ---------- primitives ---------- */

  function Avatar(profile, size, dot) {
    var a = agentOf(profile);
    return h("div", {
      style: {
        width: size, height: size, borderRadius: "50%", background: a.color,
        color: "#0b0f0d", display: "flex", alignItems: "center", justifyContent: "center",
        fontFamily: SERIF, fontWeight: 700, fontSize: Math.round(size * 0.45),
        position: "relative", flex: "none",
      },
      title: a.name,
    },
      a.initial,
      dot === undefined ? null : h("span", {
        style: {
          position: "absolute", right: -1, bottom: -1,
          width: Math.round(size * 0.3), height: Math.round(size * 0.3),
          borderRadius: "50%", background: dot ? GREEN : "var(--color-muted-foreground, #999)",
          border: "2px solid var(--color-card, #fffdf7)",
        },
      })
    );
  }

  function Chip(props) {
    return h("span", {
      style: {
        fontFamily: MONO, fontSize: 10.5, letterSpacing: "0.06em", textTransform: "uppercase",
        padding: "3px 7px", borderRadius: 3, whiteSpace: "nowrap",
        border: "1px solid " + (props.color || LINE),
        color: props.color || "inherit",
        background: props.solid ? props.color : "transparent",
        fontWeight: 600,
      },
    }, props.label);
  }

  var STATE_META = {
    running:            { label: "Working", color: WARN },
    ready_for_review:   { label: "Needs your approval", color: GREEN },
    approved:           { label: "Approved", color: GREEN },
    changes_requested:  { label: "Changes requested", color: WARN },
  };

  /* ---------- document reader ---------- */

  function Reader(props) {
    var st = useState(null); var doc = st[0]; var setDoc = st[1];
    var er = useState(null); var err = er[0]; var setErr = er[1];
    var file = props.file;

    useEffect(function () {
      var alive = true;
      setDoc(null); setErr(null);
      var url = API + "/deliverables/" + encodeURIComponent(props.id) + "/doc"
        + (file ? "?file=" + encodeURIComponent(file) : "");
      SDK.fetchJSON(url)
        .then(function (r) { if (alive) setDoc(r); })
        .catch(function (e) { if (alive) setErr(String(e && e.message ? e.message : e)); });
      return function () { alive = false; };
    }, [props.id, file]);

    return h("div", {
      style: {
        position: "fixed", inset: 0, zIndex: 60, background: "rgba(0,0,0,0.55)",
        display: "flex", alignItems: "stretch", justifyContent: "center", padding: "3vh 2vw",
      },
      onClick: props.onClose,
    },
      h("div", {
        onClick: function (e) { e.stopPropagation(); },
        style: {
          background: "var(--color-card, #fff)", border: "1px solid " + LINE, borderRadius: 6,
          width: "min(920px, 100%)", display: "flex", flexDirection: "column", overflow: "hidden",
        },
      },
        /* reader toolbar */
        h("div", {
          style: {
            display: "flex", alignItems: "center", gap: 10, padding: "10px 14px",
            borderBottom: "1px solid " + LINE, flex: "none", flexWrap: "wrap",
          },
        },
          h("strong", { style: { fontFamily: SERIF, fontSize: 15, flex: 1, minWidth: 0 } },
            doc ? doc.file : "Loading…"),
          (props.files || []).map(function (f) {
            var active = (file || (props.files.filter(function (x) { return x.is_final; })[0] || {}).name) === f.name;
            return h("button", {
              key: f.name,
              onClick: function () { props.onFile(f.name); },
              style: {
                fontFamily: MONO, fontSize: 10.5, padding: "3px 7px", borderRadius: 3,
                border: "1px solid " + (active ? GREEN : LINE),
                color: active ? GREEN : "inherit",
                background: "transparent", cursor: "pointer", fontWeight: 600,
              },
            }, f.is_final ? "FINAL" : f.name.replace(/^\d+-/, "").replace(/\.md$/, ""));
          }),
          h("button", {
            onClick: props.onClose,
            style: {
              border: "1px solid " + LINE, background: "transparent", cursor: "pointer",
              borderRadius: 3, padding: "3px 9px", fontFamily: MONO, fontSize: 11,
            },
          }, "✕ Close")
        ),
        /* body */
        h("div", { style: { overflowY: "auto", padding: "22px 26px", lineHeight: 1.6 } },
          err
            ? h("p", { style: { color: WARN } }, "Could not load: " + err)
            : !doc
              ? h("p", { style: { opacity: 0.6 } }, "Loading…")
              : h("div", {
                  className: "oae-doc",
                  dangerouslySetInnerHTML: { __html: doc.html },
                })
        )
      )
    );
  }

  /* ---------- one deliverable ---------- */

  function DeliverableCard(props) {
    var d = props.item;
    var ns = useState(""); var notes = ns[0]; var setNotes = ns[1];
    var rs = useState(false); var reopened = rs[0]; var setReopened = rs[1];
    var hs = useState(false); var showTrail = hs[0]; var setShowTrail = hs[1];
    var bs = useState(false); var busy = bs[0]; var setBusy = bs[1];
    var es = useState(null); var error = es[0]; var setError = es[1];

    var meta = STATE_META[d.state] || { label: d.state, color: LINE };
    var decided = d.state === "approved" || d.state === "changes_requested";
    // A decision must never be a dead end: reopening restores the whole choice, not just the send-back path.
    var open = d.state === "ready_for_review" || reopened;

    function decide(decision) {
      if (decision === "changes_requested" && !notes.trim()) {
        setError("Tell the agents what to change — they can't act on a blank rejection.");
        return;
      }
      setBusy(true); setError(null);
      SDK.fetchJSON(API + "/deliverables/" + encodeURIComponent(d.id) + "/decision", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ decision: decision, notes: notes }),
      })
        .then(function () { setReopened(false); setNotes(""); props.onChanged(); })
        .catch(function (e) { setError(String(e && e.message ? e.message : e)); })
        .finally(function () { setBusy(false); });
    }

    return h(C.Card, { style: { borderLeft: "3px solid " + meta.color } },
      h(C.CardContent, { className: "py-4" },
        h("div", { style: { display: "flex", flexDirection: "column", gap: 12 } },

          /* headline row */
          h("div", { style: { display: "flex", gap: 12, alignItems: "flex-start", flexWrap: "wrap" } },
            h("div", { style: { flex: 1, minWidth: 220 } },
              h("div", { style: { display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap", marginBottom: 5 } },
                Chip({ label: meta.label, color: meta.color }),
                Chip({ label: d.progress.done + "/" + d.progress.total + " cards" }),
                d.created_at ? h("span", { style: { fontSize: 11.5, opacity: 0.55, fontFamily: MONO } },
                  SDK.utils.timeAgo(d.created_at)) : null
              ),
              h("div", { style: { fontFamily: SERIF, fontSize: 18, fontWeight: 700, lineHeight: 1.25 } },
                d.title.replace(/^SWARM:\s*/i, "")),
              d.goal ? h("p", {
                style: { fontSize: 13, opacity: 0.7, margin: "5px 0 0", maxWidth: "70ch" },
              }, d.goal.split("\n")[0].slice(0, 190)) : null
            ),
            /* who built it */
            h("div", { style: { display: "flex", gap: 4, flex: "none" } },
              (d.agents || []).map(function (p) {
                return h("div", { key: p }, Avatar(p, 30));
              })
            )
          ),

          /* the decision Ahmed made, and what it set in motion */
          decided && d.decision ? h("div", {
            style: {
              fontSize: 12.5, padding: "9px 11px", borderRadius: 4,
              border: "1px solid " + meta.color, color: meta.color,
            },
          },
            h("strong", null, d.state === "approved" ? "✓ You approved this" : "↩ You sent this back"),
            d.decision.at ? h("span", { style: { opacity: 0.75 } },
              " · " + SDK.utils.timeAgo(d.decision.at)) : null,
            d.decision.notes ? h("div", { style: { marginTop: 4, opacity: 0.9 } },
              "“" + d.decision.notes + "”") : null,

            /* the job the click fired */
            d.job ? h("div", {
              style: {
                marginTop: 9, paddingTop: 9, borderTop: "1px solid " + meta.color,
                color: "var(--color-foreground, inherit)",
              },
            },
              h("div", { style: { display: "flex", alignItems: "center", gap: 8, flexWrap: "wrap" } },
                h("span", { style: { opacity: 0.7, fontSize: 12 } }, "→ Sent to"),
                Avatar(d.job.assignee, 20),
                h("strong", { style: { fontSize: 12.5 } }, agentOf(d.job.assignee).name),
                Chip({
                  label: d.job.status === "done" ? "Finished" : d.job.status,
                  color: d.job.status === "done" ? GREEN : WARN,
                }),
                h("span", { style: { fontFamily: MONO, fontSize: 10.5, opacity: 0.45 } }, d.job.id)
              ),
              d.job.summary ? h("div", {
                style: { marginTop: 6, fontSize: 12.5, opacity: 0.85, whiteSpace: "pre-wrap" },
              }, d.job.summary) : h("div", {
                style: { marginTop: 5, fontSize: 12, opacity: 0.6 },
              }, d.job.status === "done"
                  ? "Finished — no summary reported."
                  : "Working on it. This card is on your Kanban board."),
              d.state === "approved" ? h("div", {
                style: { marginTop: 7, fontSize: 11.5, opacity: 0.6, fontStyle: "italic" },
              }, "Plan-only: nothing touches GoHighLevel until you give a second, separate OK.") : null
            ) : (d.decision.job_id === null ? h("div", {
              style: { marginTop: 7, fontSize: 12, color: WARN },
            }, "⚠ Decision saved, but the follow-up job could not be created. Nobody was told.") : null)
          ) : null,

          /* your decision — on approve these notes are the payload, not an afterthought */
          open ? h("div", { style: { display: "flex", flexDirection: "column", gap: 6 } },
            h("textarea", {
              value: notes,
              onChange: function (e) { setNotes(e.target.value); },
              placeholder:
                "Your answers and instructions — these go straight to the agents.\n" +
                "Approving? Put your decisions here (focus, budget, city, lead magnet name, differentiator).\n" +
                "Sending it back? Say what to change. Required.",
              rows: 4,
              style: {
                width: "100%", padding: "8px 10px", borderRadius: 4, fontSize: 13,
                border: "1px solid " + LINE, background: "var(--color-input, transparent)",
                color: "inherit", fontFamily: "inherit", resize: "vertical",
              },
            })
          ) : null,

          /* actions */
          h("div", { style: { display: "flex", gap: 8, flexWrap: "wrap", alignItems: "center" } },
            h("button", {
              onClick: function () { props.onRead(d); },
              disabled: !d.final_file,
              style: {
                padding: "7px 14px", borderRadius: 4, cursor: d.final_file ? "pointer" : "not-allowed",
                border: "1px solid " + LINE, background: "transparent", fontWeight: 600, fontSize: 13,
                opacity: d.final_file ? 1 : 0.45,
              },
            }, "Read the work"),

            open ? h("button", {
              onClick: function () { decide("approved"); },
              disabled: busy,
              style: {
                padding: "7px 14px", borderRadius: 4, cursor: "pointer", border: "1px solid " + GREEN,
                background: GREEN, color: "#0b0f0d", fontWeight: 700, fontSize: 13,
              },
            }, busy ? "Saving…" : "✓ Approve") : null,

            open ? h("button", {
              onClick: function () { decide("changes_requested"); },
              disabled: busy,
              style: {
                padding: "7px 14px", borderRadius: 4, cursor: "pointer",
                border: "1px solid " + WARN, background: "transparent", color: WARN,
                fontWeight: 600, fontSize: 13,
              },
            }, busy ? "Saving…" : "↩ Send it back") : null,

            (decided && reopened) ? h("button", {
              onClick: function () { setReopened(false); setNotes(""); setError(null); },
              style: {
                padding: "6px 11px", borderRadius: 4, cursor: "pointer", border: "1px solid " + LINE,
                background: "transparent", fontSize: 12.5, opacity: 0.8,
              },
            }, "Cancel") : null,

            (decided && !reopened) ? h("button", {
              onClick: function () { setReopened(true); setError(null); },
              style: {
                padding: "6px 11px", borderRadius: 4, cursor: "pointer", border: "1px solid " + LINE,
                background: "transparent", fontSize: 12.5, opacity: 0.8,
              },
            }, "Change my decision") : null,

            (d.handoffs && d.handoffs.length) ? h("button", {
              onClick: function () { setShowTrail(!showTrail); },
              style: {
                marginLeft: "auto", padding: "6px 10px", borderRadius: 4, cursor: "pointer",
                border: "1px solid " + LINE, background: "transparent", fontSize: 12,
                fontFamily: MONO, opacity: 0.75,
              },
            }, (showTrail ? "▾ " : "▸ ") + d.handoffs.length + " agent handoffs") : null
          ),

          error ? h("p", { style: { color: WARN, fontSize: 12.5, margin: 0 } }, error) : null,

          /* audit trail */
          showTrail ? h("div", {
            style: { borderTop: "1px solid " + LINE, paddingTop: 10, display: "flex", flexDirection: "column", gap: 9 },
          },
            (d.handoffs || []).map(function (hh, i) {
              var a = agentOf(hh.author);
              return h("div", { key: i, style: { display: "flex", gap: 9, alignItems: "flex-start" } },
                Avatar(hh.author, 22),
                h("div", { style: { minWidth: 0 } },
                  h("div", { style: { fontSize: 12, fontWeight: 700 } },
                    a.name,
                    h("span", { style: { fontFamily: MONO, fontWeight: 400, opacity: 0.5, marginLeft: 6, fontSize: 11 } },
                      hh.key)
                  ),
                  h("div", { style: { fontSize: 12.5, opacity: 0.75, whiteSpace: "pre-wrap", wordBreak: "break-word" } },
                    hh.summary)
                )
              );
            })
          ) : null
        )
      )
    );
  }

  /* ---------- team cockpit ---------- */

  function TeamCard(props) {
    var m = props.member;
    var a = agentOf(m.profile);
    return h(C.Card, { style: { borderTop: "2px solid " + a.color } },
      h(C.CardContent, { className: "py-4" },
        h("div", { style: { display: "flex", gap: 11, alignItems: "center" } },
          Avatar(m.profile, 38, m.gateway_running),
          h("div", { style: { minWidth: 0, lineHeight: 1.3 } },
            h("div", { style: { fontWeight: 700, fontSize: 14 } }, a.name),
            h("div", { style: { fontSize: 11.5, opacity: 0.65 } }, a.role || m.profile),
            h("div", {
              style: { fontSize: 11.5, fontWeight: 700, color: m.working_on ? a.color : (m.gateway_running ? GREEN : "inherit"), opacity: m.working_on || m.gateway_running ? 1 : 0.5 },
            }, m.working_on ? "● Working" : (m.gateway_running ? "● On shift" : "○ Off shift"))
          )
        ),
        m.working_on ? h("div", {
          style: {
            marginTop: 9, fontSize: 12, opacity: 0.8, borderLeft: "2px solid " + a.color,
            paddingLeft: 8, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap",
          },
          title: m.working_on,
        }, m.working_on) : null,
        h("div", {
          style: {
            marginTop: 9, fontFamily: MONO, fontSize: 10.5, letterSpacing: "0.05em",
            textTransform: "uppercase", opacity: 0.55,
          },
        }, m.completed + " task" + (m.completed === 1 ? "" : "s") + " delivered")
      )
    );
  }

  /* ---------- header-left brand lockup ---------- */

  function BrandLockup() {
    return h("div", { style: { display: "flex", alignItems: "center", gap: 10, minWidth: 0 } },
      Avatar("default", 30, true),
      h("div", { style: { lineHeight: 1.1, minWidth: 0 } },
        h("div", {
          style: {
            fontWeight: 700, fontSize: "0.82rem", letterSpacing: "0.06em",
            textTransform: "uppercase", whiteSpace: "nowrap",
          },
        }, "One AI Employee"),
        h("div", {
          style: {
            fontSize: "0.6rem", letterSpacing: "0.14em", textTransform: "uppercase",
            opacity: 0.6, whiteSpace: "nowrap",
          },
        }, "Ahmed's Agency")
      )
    );
  }

  /* ---------- the page ---------- */

  function Tile(props) {
    return h(C.Card, null,
      h(C.CardContent, { className: "py-4" },
        h("div", {
          style: {
            fontSize: 26, fontWeight: 700, fontVariantNumeric: "tabular-nums",
            color: props.color,
          },
        }, props.value),
        h("div", {
          style: {
            fontSize: 11, letterSpacing: "0.07em", textTransform: "uppercase",
            opacity: 0.65, marginTop: 2, fontWeight: 600,
          },
        }, props.label)
      )
    );
  }

  function ModeButton(props) {
    return h("button", {
      onClick: props.onClick,
      disabled: props.disabled,
      style: {
        flex: 1, minWidth: 215, textAlign: "left", padding: "9px 11px", borderRadius: 5,
        cursor: props.disabled ? "not-allowed" : "pointer", color: "inherit",
        border: "1px solid " + (props.active ? props.color : LINE),
        background: props.active
          ? "color-mix(in srgb, " + props.color + " 12%, transparent)"
          : "transparent",
      },
    },
      h("div", { style: { display: "flex", alignItems: "center", gap: 7, marginBottom: 3 } },
        h("span", { style: { display: "flex", gap: 3 } },
          props.profiles.map(function (p) { return h("span", { key: p }, Avatar(p, 19)); })),
        h("span", { style: { fontWeight: 700, fontSize: 13 } }, props.label)
      ),
      h("div", { style: { fontSize: 11.5, opacity: 0.7, lineHeight: 1.4 } }, props.hint)
    );
  }

  function NewJob(props) {
    var qs = useState(""); var req = qs[0]; var setReq = qs[1];
    var ms = useState("team"); var mode = ms[0]; var setMode = ms[1];
    var bs = useState(false); var busy = bs[0]; var setBusy = bs[1];
    var es = useState(null); var error = es[0]; var setError = es[1];
    var ss = useState(null); var sent = ss[0]; var setSent = ss[1];

    function send() {
      if (!req.trim()) { setError("Tell your team what you need."); return; }
      setBusy(true); setError(null);
      SDK.fetchJSON(API + "/jobs", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ request: req, mode: mode }),
      })
        .then(function (r) { setSent(r); setReq(""); props.onStarted(); })
        .catch(function (e) { setError(String(e && e.message ? e.message : e)); })
        .finally(function () { setBusy(false); });
    }

    return h(C.Card, { style: { borderLeft: "3px solid " + BLUE } },
      h(C.CardContent, { className: "py-4" },
        h("div", { style: { display: "flex", flexDirection: "column", gap: 10 } },

          h("h2", { style: { fontFamily: SERIF, fontSize: 20, fontWeight: 700, margin: 0 } },
            "Give your team a job"),

          h("textarea", {
            value: req,
            onChange: function (e) { setReq(e.target.value); },
            placeholder:
              "What do you need? Write it the way you'd say it to a person.\n" +
              "e.g. \"Build a 30-day launch plan for a new dental client on GoHighLevel.\"",
            rows: 3,
            disabled: busy,
            style: {
              width: "100%", padding: "9px 11px", borderRadius: 4, fontSize: 13.5,
              border: "1px solid " + LINE, background: "var(--color-input, transparent)",
              color: "inherit", fontFamily: "inherit", resize: "vertical",
            },
          }),

          h("div", { style: { display: "flex", gap: 8, flexWrap: "wrap" } },
            ModeButton({
              active: mode === "team", disabled: busy, color: BLUE,
              onClick: function () { setMode("team"); },
              profiles: ["marketing", "creative", "ghl"],
              label: "Put the whole team on it",
              hint: "Strategy, creative and the GoHighLevel build in parallel — then verified and merged into one document. Takes ~15 minutes.",
            }),
            ModeButton({
              active: mode === "adam", disabled: busy, color: GREEN,
              onClick: function () { setMode("adam"); },
              profiles: ["default"],
              label: "Ask Adam",
              hint: "One card to your Chief of Staff. He handles it or delegates it himself. Best for anything that isn't a full campaign.",
            })
          ),

          h("div", { style: { display: "flex", gap: 9, alignItems: "center", flexWrap: "wrap" } },
            h("button", {
              onClick: send,
              disabled: busy,
              style: {
                padding: "7px 15px", borderRadius: 4, cursor: busy ? "not-allowed" : "pointer",
                border: "1px solid " + BLUE, background: BLUE, color: "#0b0f0d",
                fontWeight: 700, fontSize: 13,
              },
            }, busy ? "Starting…" : "Send it →"),
            h("span", { style: { fontSize: 11.5, opacity: 0.6 } },
              "They prepare and bring it back here. Nothing goes live without your approval.")
          ),

          error ? h("p", { style: { color: WARN, fontSize: 12.5, margin: 0 } }, error) : null,

          sent ? h("div", {
            style: {
              borderTop: "1px solid " + LINE, paddingTop: 9, fontSize: 12.5,
              display: "flex", flexDirection: "column", gap: 3,
            },
          },
            h("div", null,
              h("strong", { style: { color: GREEN } }, "✓ Started."),
              sent.mode === "team"
                ? " Your three specialists are working in parallel. Adam verifies and merges."
                : " Adam has it."
            ),
            h("span", { style: { fontFamily: MONO, fontSize: 11, opacity: 0.55 } }, sent.root_id),
            h("span", { style: { opacity: 0.7 } },
              "It'll appear below as it runs — this page refreshes itself every 15 seconds.")
          ) : null
        )
      )
    );
  }

  function TodayPage() {
    var ts = useState(null); var team = ts[0]; var setTeam = ts[1];
    var ds = useState(null); var items = ds[0]; var setItems = ds[1];
    var rs = useState(null); var reading = rs[0]; var setReading = rs[1];
    var fs = useState(null); var readFile = fs[0]; var setReadFile = fs[1];
    var es = useState(null); var loadErr = es[0]; var setLoadErr = es[1];

    var load = useCallback(function () {
      SDK.fetchJSON(API + "/team")
        .then(function (r) { setTeam(r.team || []); })
        .catch(function (e) { setLoadErr(String(e && e.message ? e.message : e)); });
      SDK.fetchJSON(API + "/deliverables")
        .then(function (r) { setItems(r.deliverables || []); })
        .catch(function (e) { setLoadErr(String(e && e.message ? e.message : e)); });
    }, []);

    useEffect(function () {
      load();
      var t = setInterval(load, 15000);   // keep the cockpit live while a swarm runs
      return function () { clearInterval(t); };
    }, [load]);

    var pending = (items || []).filter(function (d) { return d.state === "ready_for_review"; });
    var working = (items || []).filter(function (d) { return d.state === "running"; });
    var onShift = (team || []).filter(function (m) { return m.gateway_running; }).length;
    var delivered = (team || []).reduce(function (n, m) { return n + m.completed; }, 0);

    var hour = new Date().getHours();
    var greeting = hour < 12 ? "Good morning, Ahmed." : hour < 18 ? "Good afternoon, Ahmed." : "Good evening, Ahmed.";

    return h("div", { style: { display: "flex", flexDirection: "column", gap: 18, maxWidth: 1180 } },

      h("style", null,
        ".oae-doc h1{font-family:" + SERIF + ";font-size:1.7rem;margin:0 0 14px}" +
        ".oae-doc h2{font-family:" + SERIF + ";font-size:1.3rem;margin:30px 0 10px;padding-top:10px;border-top:1px solid " + LINE + "}" +
        ".oae-doc h3{font-size:1rem;margin:20px 0 6px;opacity:.85}" +
        ".oae-doc p{margin:0 0 11px}" +
        ".oae-doc ul,.oae-doc ol{margin:0 0 13px;padding-left:22px}" +
        ".oae-doc li{margin:4px 0}" +
        /* Placeholders like {{city}} are the whole point of a template, so they
           must read at a glance. --color-muted lands dark-on-dark in Nightshift;
           tint from the accent instead, which every theme defines with contrast
           against its own ground. */
        ".oae-doc code{font-family:" + MONO + ";font-size:.86em;padding:1px 5px;border-radius:3px;" +
          "color:var(--color-accent,inherit);background:color-mix(in srgb, var(--color-accent,#888) 14%, transparent);" +
          "border:1px solid color-mix(in srgb, var(--color-accent,#888) 30%, transparent)}" +
        ".oae-doc pre code{color:inherit;background:none;border:0;padding:0}" +
        ".oae-doc pre{overflow-x:auto;padding:12px;border:1px solid " + LINE + ";border-radius:4px}" +
        ".oae-doc table{border-collapse:collapse;width:100%;font-size:13px;margin:0 0 14px}" +
        ".oae-doc th,.oae-doc td{border:1px solid " + LINE + ";padding:6px 9px;text-align:left}" +
        ".oae-doc hr{border:0;border-top:1px solid " + LINE + ";margin:22px 0}"
      ),

      /* greeting */
      h("div", null,
        h("h1", { style: { fontFamily: SERIF, fontSize: 30, fontWeight: 400, margin: 0 } }, greeting),
        h("p", { style: { opacity: 0.7, fontSize: 14, marginTop: 6, maxWidth: "62ch" } },
          pending.length
            ? "Your team finished work that needs your decision. Nothing goes further without you."
            : "Your team's work lands here for your decision. Nothing goes out without your approval.")
      ),

      /* tiles */
      h("div", { style: { display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(150px, 1fr))", gap: 12 } },
        /* Brand kit semantics: amber = "needs you", green = "Adam is here",
           blue = work in flight. Waiting on Ahmed is amber, never green. */
        Tile({ value: String(pending.length), label: "Awaiting your approval", color: pending.length ? WARN : undefined }),
        Tile({ value: String(working.length), label: "In progress", color: working.length ? BLUE : undefined }),
        Tile({ value: onShift + "/" + ((team || []).length || 0), label: "Agents on shift" }),
        Tile({ value: String(delivered), label: "Tasks delivered" })
      ),

      loadErr ? h(C.Card, null, h(C.CardContent, { className: "py-4" },
        h("p", { style: { color: WARN, margin: 0, fontSize: 13 } }, "Couldn't reach the backend: " + loadErr)
      )) : null,

      /* start work */
      h(NewJob, { onStarted: load }),

      /* approvals desk */
      h("div", null,
        h("h2", {
          style: {
            fontFamily: SERIF, fontSize: 20, fontWeight: 700, margin: "0 0 10px",
            display: "flex", alignItems: "center", gap: 9,
          },
        },
          "Awaiting your approval",
          pending.length ? Chip({ label: String(pending.length), color: GREEN }) : null
        ),
        items === null
          ? h("p", { style: { opacity: 0.6, fontSize: 13.5 } }, "Loading…")
          : items.length === 0
            ? h(C.Card, null, h(C.CardContent, { className: "py-4" },
                h("p", { style: { fontSize: 13.5, opacity: 0.75, margin: 0 } },
                  "No work yet. When your agents finish something, it lands here for you to read and approve.")
              ))
            : h("div", { style: { display: "flex", flexDirection: "column", gap: 12 } },
                items.map(function (d) {
                  return h(DeliverableCard, {
                    key: d.id,
                    item: d,
                    onChanged: load,
                    onRead: function (x) { setReading(x); setReadFile(null); },
                  });
                })
              )
      ),

      /* the team */
      h("div", null,
        h("h2", { style: { fontFamily: SERIF, fontSize: 20, fontWeight: 700, margin: "0 0 10px" } },
          "Your team"),
        team === null
          ? h("p", { style: { opacity: 0.6, fontSize: 13.5 } }, "Loading…")
          : h("div", { style: { display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(210px, 1fr))", gap: 12 } },
              team.map(function (m) { return h(TeamCard, { key: m.profile, member: m }); })
            )
      ),

      reading ? h(Reader, {
        id: reading.id,
        file: readFile,
        files: reading.files,
        onFile: setReadFile,
        onClose: function () { setReading(null); setReadFile(null); },
      }) : null
    );
  }

  window.__HERMES_PLUGINS__.register("one-ai-employee", TodayPage);
  window.__HERMES_PLUGINS__.registerSlot("one-ai-employee", "header-left", BrandLockup);
})();
