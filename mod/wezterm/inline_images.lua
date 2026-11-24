-- inline-images: the WezTerm side of the overlay that draws real images in Claude Code.
--
-- The overlay (mod/bin/wezterm_overlay.py) cannot print into a pane that Claude Code
-- owns: bytes written to the pane's tty mix with Claude Code's own output, and a large
-- image then shows as base64 text. So it drops each image, as one escape sequence, in a
-- folder; this file picks it up and hands it to `pane:inject_output`, which WezTerm
-- parses on its own, between whole actions of Claude Code's output.
--
-- It also writes the screen text of each pane that an overlay watches to the folder, so
-- the overlay never calls `wezterm cli get-text`: that call sends every image in the pane
-- along with the text, and while an overlay polled it WezTerm stalled for seconds.
--
-- Use: copy this file next to your wezterm.lua and add, before `return config`:
--
--   require('inline_images').apply(config)
--
-- The folder is read on WezTerm's `update-status` event, every `status_update_interval`
-- (1 s by default). A lower interval shows images sooner: this file sets 250 ms unless
-- your config set another value first. (A timer would be quicker, but WezTerm loads the
-- config again often, and timers started while it loads fire late or not at all.)
--
-- After each image, and on every tick while an overlay watches a pane, it sets the
-- window's right status to one or two spaces: that is what makes WezTerm repaint, and
-- WezTerm fires update-status again only after a repaint. A right status of your own
-- is overwritten while Claude Code runs with the overlay.
--
-- Anything that can write to the folder could put text on your panes, so the overlay
-- keeps it private to you (0700); a process running as you can write to your ttys anyway.

local wezterm = require 'wezterm'

local M = {}

M.inbox = (os.getenv 'HOME' or '') .. '/.local/state/inline-images/wezterm'

local ALIVE_SECONDS = 2
local MAX_BYTES = 32 * 1024 * 1024

local WATCH_SECONDS = 10
local STALE_MS = 4000

local alive_at = 0
local nudge = false
local screens = {} -- pane id -> the last text written for it
local sequence = 0

local function write_atomic(path, data)
  local temporary = path .. '.part'
  local handle = io.open(temporary, 'w')
  if not handle then
    return
  end
  handle:write(data)
  handle:close()
  os.rename(temporary, path)
end

-- The viewport as text, one line per row. Not pane:get_lines_as_text: it trims the
-- whole text after each row, so blank rows vanish and every row below a blank one is
-- reported higher up than it is; the overlay then drew images, and erased cells, on the
-- wrong rows.
function M.screen_text(pane, size)
  local top = size.physical_top
  local rows = {}
  for row = top, top + size.viewport_rows - 1 do
    local ok, line = pcall(pane.get_text_from_region, pane, 0, row, size.cols, row)
    line = ok and type(line) == 'string' and line or ''
    rows[#rows + 1] = (line:gsub('\n', ''):gsub('%s+$', ''))
  end
  return table.concat(rows, '\n')
end

-- The screen of each pane whose overlay touched <id>.watch lately, as <id>.screen:
-- a line "rows columns sequence", then the rows. Written only when it changed.
function M.screens(inbox)
  local now = os.time()
  local watched = false
  for _, path in ipairs(wezterm.glob(inbox .. '/*.watch')) do
    local id = tonumber(path:match '/(%d+)%.watch$')
    local handle = io.open(path, 'r')
    local stamp = handle and tonumber(handle:read 'l')
    if handle then
      handle:close()
    end
    if id and stamp and now - stamp <= WATCH_SECONDS then
      watched = true
      local found, pane = pcall(wezterm.mux.get_pane, id)
      if found and pane then
        local size = pane:get_dimensions()
        local text = M.screen_text(pane, size)
        local key = size.cols .. 'x' .. size.viewport_rows .. '\n' .. text
        if screens[id] ~= key then
          screens[id] = key
          sequence = sequence + 1
          write_atomic(inbox .. '/' .. id .. '.screen', string.format('%d %d %d\n', size.viewport_rows, size.cols, sequence) .. text)
        end
      end
    else
      screens[id or -1] = nil
    end
  end
  return watched
end

-- Inject every waiting sequence into its pane.
function M.drain(inbox)
  local files = wezterm.glob(inbox .. '/*.osc')
  table.sort(files)
  local injected = {}
  for _, path in ipairs(files) do
    local id = tonumber(path:match '/(%d+)%-[^/]*%.osc$')
    local handle = io.open(path, 'rb')
    local data = handle and handle:read 'a'
    if handle then
      handle:close()
    end
    os.remove(path)
    -- An image that waited too long (WezTerm was hidden) may no longer fit the screen:
    -- the overlay draws it again where its box is now.
    local posted = tonumber(path:match '%-(%d+)%.osc$')
    local fresh = posted and (os.time() * 1000 - posted / 1000) < STALE_MS
    local found, pane = pcall(wezterm.mux.get_pane, id)
    if id and fresh and data and #data <= MAX_BYTES and found and pane then
      pane:inject_output(data)
      injected[#injected + 1] = pane
    end
  end

  -- inject_output changes the pane but does not repaint the window: a new right-status
  -- text does. One space, then two, so each nudge differs from the last.
  for _, pane in ipairs(injected) do
    local window = pane:window()
    local gui = window and window:gui_window()
    if gui then
      nudge = not nudge
      gui:set_right_status(nudge and ' ' or '  ')
    end
  end

  -- Tell the overlay this side is here. The overlay makes the folder.
  local now = os.time()
  if now - alive_at >= ALIVE_SECONDS then
    local handle = io.open(inbox .. '/alive', 'w')
    if handle then
      handle:write(tostring(now))
      handle:close()
      alive_at = now
    end
  end
end

function M.apply(config, opts)
  opts = opts or {}
  local inbox = opts.inbox or os.getenv 'INLINE_IMAGES_WEZTERM_INBOX' or M.inbox

  if config.status_update_interval == nil then
    config.status_update_interval = 250
  end

  wezterm.on('update-status', function(window, pane)
    local ok, problem = pcall(M.drain, inbox)
    local watched = false
    if ok then
      ok, watched = pcall(M.screens, inbox)
      problem = watched
    end
    if not ok then
      wezterm.log_warn('inline-images: ' .. tostring(problem))
    end
    -- WezTerm fires update-status again only after the window repaints. While an
    -- overlay watches a pane, repaint every tick (a new right status does it), or the
    -- screen and the images stop flowing as soon as Claude Code goes idle.
    if watched == true then
      nudge = not nudge
      window:set_right_status(nudge and ' ' or '  ')
    end
  end)
  return config
end

return M
