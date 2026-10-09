import type { ClientModule } from 'claude-code'

// A click layer over a preview. It draws nothing, so the picture under it
// shows, and it tells the hooks module when the left button goes down and up
// inside it. The hooks module knows which file this layer stands for (by the
// drawing and this layer's key) and opens it in the system's own viewer: the
// message carries no path, so nothing here can choose what is opened.

type State = { isListening: true }

const Click: ClientModule<null, State> = (_props, surface) => {
  if (surface.state === undefined) {
    let isDown = false

    surface.onPointer(event => {
      const isInside = event.x >= 0 && event.y >= 0 && event.x < surface.columns && event.y < surface.rows

      if (event.type === 'down') {
        isDown = event.button === 'left' && isInside
      } else if (event.type === 'up') {
        if (isDown && isInside) {
          surface.post({ open: true })
        }

        isDown = false
      } else if (event.type === 'leave') {
        isDown = false
      }
    })
    surface.setState({ isListening: true })
  }

  return surface.elements.Box({ width: surface.columns, height: surface.rows })
}

export default Click
