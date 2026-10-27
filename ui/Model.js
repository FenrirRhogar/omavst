.pragma library

// Pure helpers shared by the bar widget and the panel.

// Nerd Font glyphs (Omarchy themes ship a Nerd Font).
var GLYPH_OK = "󰝚"       // music note
var GLYPH_OFFLINE = "󰝛"  // music note off
var GLYPH_SYNC = "󰓦"
var GLYPH_REFRESH = "󰑐"
var GLYPH_DISABLE = "󰒲"  // sleep
var GLYPH_ENABLE = "󰐊"   // play
var GLYPH_TRASH = "󰩺"

function counts(inv) {
  return inv && inv.counts ? inv.counts : { windows: 0, native: 0, unsynced: 0, disabled: 0 }
}

function yabridgeError(inv) {
  var y = inv ? inv.yabridge : null
  if (!y || y.ok) return ""
  return y.error && y.error.message ? String(y.error.message) : "yabridge unavailable"
}

// Something the user should act on: unsynced plugins or a broken yabridge.
function needsAttention(inv) {
  return counts(inv).unsynced > 0 || yabridgeError(inv) !== ""
}

function summary(inv) {
  var c = counts(inv)
  var parts = [c.windows + " Windows", c.native + " native"]
  if (c.disabled) parts.push(c.disabled + " disabled")
  return parts.join(" · ")
}

function attentionText(inv) {
  var err = yabridgeError(inv)
  if (err) return err
  var n = counts(inv).unsynced
  return n ? n + (n === 1 ? " plugin needs" : " plugins need") + " a yabridge sync" : ""
}

function sourceLabel(p) {
  return p.source === "windows" ? "Windows" : "Native"
}

function statusLabel(p) {
  if (p.status === "disabled") return "disabled"
  if (p.status === "unsynced") return "not synced"
  return ""
}

function resultText(result) {
  if (!result) return ""
  if (!result.ok) return (result.error && result.error.message) ? String(result.error.message) : "Action failed"
  if (result.message) return String(result.message)
  // yabridgectl sync: "Finished setting up 10 plugins (0 new), ..." is the line worth showing.
  var lines = String(result.output || "").split("\n")
  for (var i = lines.length - 1; i >= 0; i--) {
    if (lines[i].trim() !== "") return lines[i].trim()
  }
  return "Done"
}

function removeWarning(p) {
  return p && p.source === "windows"
    ? "Moves the plugin file to Trash. Presets and licences stay; use the vendor's uninstaller for a full uninstall."
    : "Moves the plugin to Trash. You can restore it from there."
}

var GLYPH_INSTALL = "󰇚"
var GLYPH_BACKUP = "󰁯"
var GLYPH_CRASH = "󰈸"

function formatSize(bytes) {
  var n = Number(bytes) || 0
  if (n >= 1073741824) return (n / 1073741824).toFixed(1) + " GB"
  if (n >= 1048576) return Math.round(n / 1048576) + " MB"
  if (n >= 1024) return Math.round(n / 1024) + " KB"
  return n + " B"
}

function formatDate(seconds) {
  if (!seconds) return ""
  var d = new Date(Number(seconds) * 1000)
  function pad(x) { return x < 10 ? "0" + x : "" + x }
  return d.getFullYear() + "-" + pad(d.getMonth() + 1) + "-" + pad(d.getDate()) + " " + pad(d.getHours()) + ":" + pad(d.getMinutes())
}

// Case-insensitive match on name, display name and vendor.
function matches(p, query) {
  var q = String(query || "").trim().toLowerCase()
  if (q === "") return true
  var hay = [p.name, p.displayName, p.vendor, p.format, sourceLabel(p)].join(" ").toLowerCase()
  var words = q.split(/\s+/)
  for (var i = 0; i < words.length; i++) {
    if (hay.indexOf(words[i]) === -1) return false
  }
  return true
}

function title(p) {
  return String(p.displayName || p.name || "")
}

function detailLines(p, d) {
  if (!d) return ["Loading…"]
  if (d.missing) return ["File is missing: " + d.path]
  var lines = []
  var head = []
  if (d.vendor) head.push(d.vendor)
  if (d.version) head.push("version " + d.version)
  if (d.size !== undefined) head.push(formatSize(d.size))
  if (d.modified) head.push("modified " + formatDate(d.modified))
  if (head.length) lines.push(head.join(" · "))
  lines.push(d.path)
  if (d.bridgedPath) lines.push("bridged: " + d.bridgedPath)
  if (p.crashes) lines.push("Crashed its Wine host " + p.crashes + (p.crashes === 1 ? " time" : " times") + " (crash watch)")
  return lines
}

function resultNote(result) {
  return result && result.reaperNote ? String(result.reaperNote) : ""
}

function jobText(job) {
  if (!job) return ""
  if (job.state === "running") return String(job.message || job.label)
  return String(job.message || "")
}
