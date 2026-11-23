-- inline-images: the WezTerm side of the overlay that draws real images in Claude Code.
--
-- The overlay (mod/bin/wezterm_overlay.py) cannot print into a pane that Claude Code
-- owns: bytes written to the pane's tty mix with Claude Code's own output, and a large
-- image then shows as base64 text. So it drops each image, as one escape sequence, in a
-- folder; this file picks it up and hands it to `pane:inject_output`, which WezTerm
-- parses on its own, between whole actions of Claude Code's output.
--
-- Use: copy this file next to your wezterm.lua and add, before `return config`:
--
--   require('inline_images').apply(config)
--
-- Anything that can write to the folder could put text on your panes, so the overlay
-- keeps it private to you (0700); a process running as you can write to your ttys anyway.

local wezterm = require 'wezterm'

local M = {}

M.inbox = (os.getenv 'HOME' or '') .. '/.local/state/inline-images/wezterm'

local TICK_SECONDS = 0.05
local ALIVE_SECONDS = 2
local MAX_BYTES = 32 * 1024 * 1024

function M.apply(config, opts)
  -- Only the GUI owns panes; `wezterm cli` and the mux server load this config too.
  if not wezterm.gui then
    return config
  end
  opts = opts or {}
  local inbox = opts.inbox or os.getenv 'INLINE_IMAGES_WEZTERM_INBOX' or M.inbox

  -- One loop, however often the config is loaded again: an older loop sees a newer
  -- generation and stops.
  wezterm.GLOBAL.inline_images_generation = (wezterm.GLOBAL.inline_images_generation or 0) + 1
  local generation = wezterm.GLOBAL.inline_images_generation
  local alive_at = 0
  local flip = false

  local function tick()
    if wezterm.GLOBAL.inline_images_generation ~= generation then
      return
    end
    local ok, problem = pcall(function()
      local files = wezterm.glob(inbox .. '/*.osc')
      local injected = {}
      table.sort(files)
      for _, path in ipairs(files) do
        local id = tonumber(path:match '/(%d+)%-[^/]*%.osc$')
        local handle = io.open(path, 'rb')
        local data = handle and handle:read 'a'
        if handle then
          handle:close()
        end
        os.remove(path)
        local found, pane = pcall(wezterm.mux.get_pane, id)
        if id and data and #data <= MAX_BYTES and found and pane then
          pane:inject_output(data)
          injected[pane] = true
        end
      end
      -- inject_output changes the pane but does not repaint the window: a new status
      -- text does. A space at the far right of the tab bar, then nothing, then a space...
      for pane in pairs(injected) do
        local window = pane:window()
        local gui = window and window:gui_window()
        if gui then
          flip = not flip
          gui:set_right_status(flip and ' ' or '')
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
    end)
    if not ok then
      wezterm.log_warn('inline-images: ' .. tostring(problem))
    end
    wezterm.time.call_after(TICK_SECONDS, tick)
  end

  wezterm.time.call_after(TICK_SECONDS, tick)
  return config
end

return M
