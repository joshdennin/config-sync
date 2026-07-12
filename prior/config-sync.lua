#!/usr/bin/env luajit
-- config-sync.lua — sync dotfile configs from git repos into place.
-- Target runtime: Lua 5.1 / LuaJIT (matches Neovim). See config-plan.md.

----------------------------------------------------------------------
-- Helpers
----------------------------------------------------------------------

local HOME = os.getenv("HOME")

-- Run a shell command; true iff it exited 0. Route every os.execute through
-- this — the nonzero return isn't a clean exit code in 5.1.
local function run(cmd)
  return os.execute(cmd) == 0
end

-- Strip leading/trailing whitespace (io.popen reads carry a trailing \n).
local function trim(s)
  return (tostring(s or ""):gsub("^%s+", ""):gsub("%s+$", ""))
end

-- Single-quote-wrap a value for the shell: ' -> '\''. Every interpolated
-- argument passes through this, so paths with spaces/metacharacters are safe.
local function escape_single_quote(s)
  return "'" .. tostring(s):gsub("'", "'\\''") .. "'"
end

-- Expand a leading ~ to $HOME. Must happen in Lua: shq single-quotes every
-- argument, so the shell never expands ~ itself.
local function expand(path)
  if path == "~" then return HOME end
  if path:sub(1, 2) == "~/" then return HOME .. path:sub(2) end
  return path
end

-- Read the full stdout of a command, trimmed. In 5.1 pipe:close() carries no
-- exit status, so these are judged by stdout alone: "" means absent/failed.
local function capture(cmd)
  local pipe = io.popen(cmd)
  local out = pipe:read("*a")
  pipe:close()
  return trim(out)
end

local function path_exists(p) return run("test -e " .. escape_single_quote(p)) end
local function is_dir(p)      return run("test -d " .. escape_single_quote(p)) end
local function is_symlink(p)  return run("test -L " .. escape_single_quote(p)) end

local function dir_empty(p)
  return capture("ls -A " .. escape_single_quote(p) .. " 2>/dev/null") == ""
end

local function read_link(p)
  return capture("readlink " .. escape_single_quote(p) .. " 2>/dev/null")
end

local function git_origin(dir)
  return capture("git -C " .. escape_single_quote(dir) .. " remote get-url origin 2>/dev/null")
end

local function have(bin)
  return run("command -v " .. escape_single_quote(bin) .. " >/dev/null 2>&1")
end

-- Compare repo URLs ignoring trailing whitespace and a trailing ".git".
local function norm_url(u)
  return (trim(u):gsub("%.git$", ""))
end

-- Rename path aside to <path>.bak.<timestamp>. Returns ok, backup_path, err.
local function backup(path)
  local dest = path .. ".bak." .. os.date("%Y%m%d-%H%M%S")
  local ok, err = os.rename(path, dest)
  return ok, dest, err
end

-- Yes/no prompt with a stated default applied on empty input.
local function prompt_yn(question, default_yes)
  io.write(question .. (default_yes and " [Y/n] " or " [y/N] "))
  io.flush()
  local answer = trim(io.read("*l")):lower()
  if answer == "" then return default_yes end
  return answer == "y" or answer == "yes"
end

----------------------------------------------------------------------
-- Result tracking
----------------------------------------------------------------------

local results = {}  -- list of { program, outcome, detail }

local function record(program, outcome, detail)
  results[#results + 1] = { program = program, outcome = outcome, detail = detail }
end

-- Surface an error (operation + target + message) and record a failure. The
-- run continues to the next entry.
local function fail(program, operation, target, msg)
  io.stderr:write(string.format("[%s] %s failed on %s: %s\n",
    program, operation, target, trim(msg)))
  record(program, "failed", operation .. " " .. target)
end

----------------------------------------------------------------------
-- Config loading (sandboxed)
----------------------------------------------------------------------

-- loadfile + setfenv(chunk, {}): the config only needs to `return` a table, so
-- no globals are exposed. Missing file / syntax error -> whole-run abort.
local function load_config(path)
  local chunk, err = loadfile(path)
  if not chunk then return nil, err end
  setfenv(chunk, {})
  local ok, result = pcall(chunk)
  if not ok then return nil, result end
  if type(result) ~= "table" then
    return nil, "config did not return a table"
  end
  return result
end

----------------------------------------------------------------------
-- Bootstrap / dependency preflight
----------------------------------------------------------------------

-- Arch-only: suggest the exact pacman command; never auto-install. Adding a
-- dependency is just another row here.
local DEPENDENCIES = {
  { bin = "git", pkg = "git" },
}

local function preflight()
  for _, dep in ipairs(DEPENDENCIES) do
    if not have(dep.bin) then
      io.stderr:write(string.format(
        "Missing dependency: %s\nInstall it with:\n  sudo pacman -S %s\n",
        dep.bin, dep.pkg))
      return false
    end
  end
  return true
end

----------------------------------------------------------------------
-- Known config paths (pre-flight probe)
----------------------------------------------------------------------

local KNOWN_PATHS = {
  nvim = { "~/.config/nvim", "~/.vim", "~/.vimrc" },
  tmux = { "~/.tmux.conf", "~/.config/tmux" },
  -- ...extend as needed
}

local STAGING_ROOT = expand("~/.local/share/config-sync")

-- Warn about existing configs at other known locations; return whether to
-- proceed with this entry.
local function probe_known_paths(entry, dest)
  local list = KNOWN_PATHS[entry.program]
  if not list then
    print(string.format("[%s] no known-paths entry; skipping probe.", entry.program))
    return true
  end
  local found = {}
  for _, p in ipairs(list) do
    local ep = expand(p)
    if ep ~= dest and path_exists(ep) then
      found[#found + 1] = ep
    end
  end
  if #found == 0 then return true end
  print(string.format("[%s] existing config found at:", entry.program))
  for _, p in ipairs(found) do print("  " .. p) end
  return prompt_yn(string.format("[%s] proceed with this entry anyway?", entry.program), false)
end

----------------------------------------------------------------------
-- Sync models
----------------------------------------------------------------------

local function git_clone(entry, repo, dir, ok_detail)
  if run("git clone " .. escape_single_quote(repo) .. " " .. escape_single_quote(dir)) then
    record(entry.program, "synced", ok_detail)
    return true
  end
  fail(entry.program, "git clone", dir, "clone failed")
  return false
end

local function make_symlink(entry, target, dest, ok_detail)
  if run("ln -s " .. escape_single_quote(target) .. " " .. escape_single_quote(dest)) then
    record(entry.program, "synced", ok_detail)
    return true
  end
  fail(entry.program, "ln -s", dest, "symlink failed")
  return false
end

-- mode = "direct": the repo IS the config; clone into dest.
local function sync_direct(entry, dest)
  local repo = entry.repo

  if not path_exists(dest) then
    if not prompt_yn(string.format("[%s] %s does not exist. Create and clone?",
        entry.program, dest), true) then
      record(entry.program, "skipped", "create declined")
      return
    end
    git_clone(entry, repo, dest, "cloned to " .. dest)
    return
  end

  if is_dir(dest) and dir_empty(dest) then
    git_clone(entry, repo, dest, "cloned into empty " .. dest)
    return
  end

  if is_dir(dest) and norm_url(git_origin(dest)) == norm_url(repo) then
    if run("git -C " .. escape_single_quote(dest) .. " pull") then
      record(entry.program, "pulled", "pulled " .. dest)
    else
      fail(entry.program, "git pull", dest, "pull failed")
    end
    return
  end

  -- other content
  if not prompt_yn(string.format("[%s] %s has other content. Back up and overwrite?",
      entry.program, dest), false) then
    record(entry.program, "skipped", "overwrite declined")
    return
  end
  local ok, bak, err = backup(dest)
  if not ok then
    fail(entry.program, "backup", dest, err)
    return
  end
  print(string.format("[%s] backed up to %s", entry.program, bak))
  git_clone(entry, repo, dest, "cloned to " .. dest .. " (backed up old)")
end

-- mode = "symlink": clone into staging, then link into place.
local function sync_symlink(entry, dest)
  local repo = entry.repo
  local staging = STAGING_ROOT .. "/" .. entry.program

  -- 1. Bring the staging clone up to date.
  if not path_exists(staging) then
    if not run("git clone " .. escape_single_quote(repo) .. " " .. escape_single_quote(staging)) then
      fail(entry.program, "git clone", staging, "clone failed")
      return
    end
  elseif norm_url(git_origin(staging)) == norm_url(repo) then
    if not run("git -C " .. escape_single_quote(staging) .. " pull") then
      fail(entry.program, "git pull", staging, "pull failed")
      return
    end
  else
    fail(entry.program, "staging", staging, "exists but origin does not match repo")
    return
  end

  -- 2. Determine the intended link target.
  local source = entry.source
  local target = (source == nil or source == "all")
    and staging or (staging .. "/" .. source)

  -- 3. Reconcile the link at dest.
  if is_symlink(dest) then
    if read_link(dest) == target then
      record(entry.program, "unchanged", "symlink already correct")
      return
    end
    local ok, err = os.remove(dest)
    if not ok then
      fail(entry.program, "remove symlink", dest, err)
      return
    end
    make_symlink(entry, target, dest, "symlink re-created " .. dest .. " -> " .. target)
    return
  end

  if not path_exists(dest) then
    local parent = dest:match("^(.*)/[^/]+$")
    if parent and parent ~= "" and not run("mkdir -p " .. escape_single_quote(parent)) then
      fail(entry.program, "mkdir -p", parent, "mkdir failed")
      return
    end
    make_symlink(entry, target, dest, "symlink created " .. dest .. " -> " .. target)
    return
  end

  if is_dir(dest) and dir_empty(dest) then
    local ok, err = os.remove(dest)
    if not ok then
      fail(entry.program, "remove empty dir", dest, err)
      return
    end
    make_symlink(entry, target, dest, "symlink created " .. dest .. " -> " .. target)
    return
  end

  -- other content
  if not prompt_yn(string.format("[%s] %s has other content. Back up and replace with symlink?",
      entry.program, dest), false) then
    record(entry.program, "skipped", "overwrite declined")
    return
  end
  local ok, bak, err = backup(dest)
  if not ok then
    fail(entry.program, "backup", dest, err)
    return
  end
  print(string.format("[%s] backed up to %s", entry.program, bak))
  make_symlink(entry, target, dest,
    "symlink created " .. dest .. " -> " .. target .. " (backed up old)")
end

----------------------------------------------------------------------
-- Per-entry flow
----------------------------------------------------------------------

local function process_entry(entry)
  local program = entry.program or "?"

  if entry.mode ~= "direct" and entry.mode ~= "symlink" then
    fail(program, "validate", program, "invalid mode: " .. tostring(entry.mode))
    return
  end

  local dest = expand(entry.dest)

  if not probe_known_paths(entry, dest) then
    record(program, "skipped", "known-path declined")
    return
  end

  if entry.mode == "direct" then
    sync_direct(entry, dest)
  else
    sync_symlink(entry, dest)
  end
end

----------------------------------------------------------------------
-- Summary
----------------------------------------------------------------------

local function print_summary()
  local order = { "synced", "pulled", "unchanged", "skipped", "failed" }
  local by = {}
  for _, r in ipairs(results) do
    by[r.outcome] = by[r.outcome] or {}
    table.insert(by[r.outcome], r)
  end

  print("\n=== Summary ===")
  for _, outcome in ipairs(order) do
    for _, r in ipairs(by[outcome] or {}) do
      print(string.format("  %-10s %s%s", outcome, r.program,
        r.detail and ("  (" .. r.detail .. ")") or ""))
    end
  end

  local flagged = #(by.skipped or {}) + #(by.failed or {})
  if flagged > 0 then
    print(string.format("\n%d %s attention (skipped/failed above).",
      flagged, flagged == 1 and "entry needs" or "entries need"))
  end
end

----------------------------------------------------------------------
-- Main
----------------------------------------------------------------------

local function main()
  if not preflight() then os.exit(1) end

  local config_path = arg[1] or expand("~/.config/config-sync/config.lua")
  local config, err = load_config(config_path)
  if not config then
    io.stderr:write("Failed to load config (" .. config_path .. "): "
      .. tostring(err) .. "\n")
    os.exit(1)
  end

  for _, entry in ipairs(config) do
    process_entry(entry)
  end

  print_summary()
end

main()
