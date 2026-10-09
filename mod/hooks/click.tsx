import type { ClientModule } from 'claude-code'

// A click layer over a preview. It draws nothing, so the picture under it
// shows, and it tells the hooks module when the left button goes down and up
// inside it: the hooks module then opens the file in the system's own viewer.

type Props = { path: string }
type State = { latest: Props }

const Click: ClientModule<Props, State> = (props, surface) => {
  if (surface.state === undefined) {
    // One listener for the life of the instance; it reads the props it was last given.
    const latest = { ...props }
    let isDown = false

    surface.onPointer(event => {
      const isInside = event.x >= 0 && event.y >= 0 && event.x < surface.columns && event.y < surface.rows

      if (event.type === 'down') {
        isDown = event.button === 'left' && isInside
      } else if (event.type === 'up') {
        if (isDown && isInside) {
          surface.post({ open: latest.path })
        }

        isDown = false
      } else if (event.type === 'leave') {
        isDown = false
      }
    })
    surface.setState({ latest })
  } else {
    surface.state.latest.path = props.path
  }

  return surface.elements.Box({ width: surface.columns, height: surface.rows })
}

export default Click
