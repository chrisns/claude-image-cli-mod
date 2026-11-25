"""Draw a 3D model (STL, OBJ, 3MF, PLY) as one picture of four views.

render.py's `model` command calls this. Only the Python standard library and
Pillow: the mod asks nothing more of the machine than it does for images.

The four views are front, right, top (all orthographic, at one scale, so a part
keeps its size from view to view) and an isometric view from the front right,
above. Z is up, as in slicers and CAD; `up="y"` turns a Y-up model first.

Drawing is the painter's way: every triangle that faces the camera, far to near.
Whole triangles sorted that way go wrong on CAD meshes, where one big flat face
(a plate) is drawn over a small part standing on it (a boss). So triangles larger
than a few pixels on screen are cut into smaller ones first, and the sort is then
right to within a few pixels. A large mesh (a scan, a sculpt) is thinned first:
the corners in each cell of a fine grid become one, at their average.
"""

import math
import os
import re
import struct
import zipfile
import xml.etree.ElementTree as ElementTree

from PIL import Image, ImageDraw, ImageFont

EXTENSIONS = (".stl", ".obj", ".3mf", ".ply")

MAX_FILE_BYTES = 512 * 1024 * 1024  # on disk
MAX_UNZIPPED_BYTES = 1024 * 1024 * 1024  # the XML of a 3MF, all parts together
MAX_TRIANGLES = 5_000_000  # read
DRAW_TRIANGLES = 200_000  # above this, the mesh is thinned before drawing
GRID = 700  # the first thinning grid: cells along the longest side of the model
MIN_GRID = 80  # the coarsest it gets, for a dense scan

TILE = 480  # pixels per view in the picture
SCALE = 2  # drawn this many times larger, then shrunk: smooth edges
SPLIT_PIXELS = 10 * SCALE  # triangles wider or taller on screen than this are cut
MARGIN = 0.08  # of a view, left empty around the model

BACKGROUND = (30, 30, 30)
TILE_BACKGROUND = (38, 40, 44)
LINE = (70, 72, 78)
MODEL = (178, 198, 224)
EDGE = (52, 62, 76)
CREASE_DEGREES = 32  # an edge between faces bent more than this is drawn as a line
TEXT = (200, 200, 200)
TEXT_DIM = (140, 140, 140)


class ModelError(Exception):
    """A model that cannot be read or drawn, with the reason in words."""


# ---------------------------------------------------------------------------
# Reading: each reader gives a flat list of floats, nine per triangle.


def read_model(path):
    """The triangles of the model at `path`, as a flat list of floats (x, y, z of three corners each)."""
    extension = os.path.splitext(path)[1].lower()

    if extension not in EXTENSIONS:
        raise ModelError("not a model file (%s)" % ", ".join(EXTENSIONS))

    size = os.path.getsize(path)

    if size > MAX_FILE_BYTES:
        raise ModelError("the file is larger than %d MB" % (MAX_FILE_BYTES // 1024 // 1024))

    if size == 0:
        raise ModelError("the file is empty")

    reader = {".stl": read_stl, ".obj": read_obj, ".3mf": read_3mf, ".ply": read_ply}[extension]
    soup = reader(path)

    if not soup:
        raise ModelError("the file holds no triangles")

    return soup


def check_count(triangles):
    if triangles > MAX_TRIANGLES:
        raise ModelError("more than %d triangles" % MAX_TRIANGLES)


def read_stl(path):
    size = os.path.getsize(path)

    with open(path, "rb") as handle:
        head = handle.read(84)

        # A binary STL may start with "solid" too: its size is what tells.
        if len(head) == 84:
            count = struct.unpack("<I", head[80:84])[0]

            if 84 + 50 * count == size:
                check_count(count)
                data = handle.read()
                soup = []
                extend = soup.extend

                for record in struct.iter_unpack("<12fH", data):
                    extend(record[3:12])

                return soup

    with open(path, "r", errors="replace") as handle:
        text = handle.read()

    numbers = re.findall(r"vertex\s+(\S+)\s+(\S+)\s+(\S+)", text)

    if len(numbers) % 3:
        raise ModelError("a broken ASCII STL")

    check_count(len(numbers) // 3)

    try:
        return [float(value) for corner in numbers for value in corner]
    except ValueError:
        raise ModelError("a broken ASCII STL")


def read_obj(path):
    vertices = []
    soup = []

    def corner(token):
        index = int(token.split("/", 1)[0])
        return vertices[index - 1] if index > 0 else vertices[index]

    with open(path, "r", errors="replace") as handle:
        for line in handle:
            if line.startswith("v "):
                parts = line.split()
                vertices.append((float(parts[1]), float(parts[2]), float(parts[3])))
            elif line.startswith("f "):
                try:
                    corners = [corner(token) for token in line.split()[1:]]
                except (ValueError, IndexError):
                    raise ModelError("a face names a corner that the file does not have")

                # A face of more corners is a fan of triangles.
                for index in range(1, len(corners) - 1):
                    soup.extend(corners[0])
                    soup.extend(corners[index])
                    soup.extend(corners[index + 1])

                if len(soup) > MAX_TRIANGLES * 9:
                    check_count(len(soup) // 9)

    return soup


def local(tag):
    return tag.rsplit("}", 1)[-1]


def parse_matrix(text):
    """A 3MF transform, "m00 m01 m02 m10 m11 m12 m20 m21 m22 m30 m31 m32", as 12 floats."""
    if not text:
        return None

    values = [float(value) for value in text.split()]

    if len(values) != 12:
        raise ModelError("a 3MF transform without 12 numbers")

    return values


def combine(outer, inner):
    """The transform `inner` then `outer` (row vectors: point * inner * outer)."""
    if inner is None:
        return outer

    if outer is None:
        return inner

    a = inner
    b = outer
    result = []

    for row in range(4):
        r = a[row * 3 : row * 3 + 3]

        for column in range(3):
            value = r[0] * b[column] + r[1] * b[3 + column] + r[2] * b[6 + column]

            if row == 3:
                value += b[9 + column]

            result.append(value)

    return result


def read_3mf(path):
    try:
        archive = zipfile.ZipFile(path)
    except zipfile.BadZipFile:
        raise ModelError("not a 3MF file (not a zip archive)")

    with archive:
        parts = [info for info in archive.infolist() if info.filename.lower().endswith(".model")]

        if sum(info.file_size for info in parts) > MAX_UNZIPPED_BYTES:
            raise ModelError("the 3MF unpacks to more than %d MB" % (MAX_UNZIPPED_BYTES // 1024 // 1024))

        # Every object of every model part: its mesh, or the components it is made of.
        objects = {}  # (part, id) -> {"mesh": (vertices, triangles)} or {"components": [...]}
        builds = []  # (part, objectid, transform) of the main part's build items
        names = ["/" + info.filename.lstrip("/") for info in parts]
        main = next((name for name in names if name.lower() == "/3d/3dmodel.model"), names[0] if names else None)
        total = 0

        for info in parts:
            name = "/" + info.filename.lstrip("/")

            with archive.open(info) as handle:
                current = None
                vertices = []
                triangles = []
                components = []

                for event, element in ElementTree.iterparse(handle, events=("start", "end")):
                    tag = local(element.tag)

                    if event == "start":
                        if tag == "object":
                            current = element.get("id")
                            vertices, triangles, components = [], [], []
                        continue

                    if tag == "vertex":
                        vertices.append((float(element.get("x")), float(element.get("y")), float(element.get("z"))))
                    elif tag == "triangle":
                        triangles.append((int(element.get("v1")), int(element.get("v2")), int(element.get("v3"))))
                        total += 1

                        if total > MAX_TRIANGLES:
                            check_count(total)
                    elif tag == "component":
                        target = element.get("{http://schemas.microsoft.com/3dmanufacturing/production/2015/06}path")
                        components.append(("/" + target.lstrip("/") if target else name, element.get("objectid"), parse_matrix(element.get("transform"))))
                    elif tag == "object":
                        if triangles:
                            objects[(name, current)] = {"mesh": (vertices, triangles)}
                        elif components:
                            objects[(name, current)] = {"components": components}
                        current = None
                    elif tag == "item":
                        builds.append((name, element.get("objectid"), parse_matrix(element.get("transform"))))

                    if tag in ("vertex", "triangle", "component", "item"):
                        element.clear()

        # Only the main part's build says what is printed; parts are libraries.
        items = [item for item in builds if item[0] == main] or builds

        if not items:
            items = [(part, key, None) for (part, key) in objects]

        soup = []

        def emit(part, key, transform, depth=0):
            if depth > 16:
                raise ModelError("the 3MF components nest too deep")

            found = objects.get((part, key))

            if found is None:
                return

            if "components" in found:
                for child_part, child_key, child_transform in found["components"]:
                    emit(child_part, child_key, combine(transform, child_transform), depth + 1)
                return

            vertices, triangles = found["mesh"]

            if transform is not None:
                m = transform

                # A mirroring transform turns the part's corners clockwise: turn them back.
                if m[0] * (m[4] * m[8] - m[5] * m[7]) - m[1] * (m[3] * m[8] - m[5] * m[6]) + m[2] * (m[3] * m[7] - m[4] * m[6]) < 0:
                    triangles = [(a, c, b) for a, b, c in triangles]

                vertices = [
                    (
                        x * m[0] + y * m[3] + z * m[6] + m[9],
                        x * m[1] + y * m[4] + z * m[7] + m[10],
                        x * m[2] + y * m[5] + z * m[8] + m[11],
                    )
                    for x, y, z in vertices
                ]

            try:
                for a, b, c in triangles:
                    soup.extend(vertices[a])
                    soup.extend(vertices[b])
                    soup.extend(vertices[c])
            except IndexError:
                raise ModelError("a 3MF triangle names a corner that the mesh does not have")

            if len(soup) > MAX_TRIANGLES * 9:
                check_count(len(soup) // 9)

        for part, key, transform in items:
            emit(part, key, transform)

        return soup


PLY_TYPES = {
    "char": "b", "int8": "b", "uchar": "B", "uint8": "B",
    "short": "h", "int16": "h", "ushort": "H", "uint16": "H",
    "int": "i", "int32": "i", "uint": "I", "uint32": "I",
    "float": "f", "float32": "f", "double": "d", "float64": "d",
}


def read_ply(path):
    with open(path, "rb") as handle:
        if handle.readline().strip() != b"ply":
            raise ModelError("not a PLY file")

        form = None
        elements = []  # (name, count, [(property name, type, list count type or None)])

        while True:
            line = handle.readline()

            if not line:
                raise ModelError("a PLY header without end_header")

            words = line.decode("ascii", "replace").split()

            if not words:
                continue

            if words[0] == "format":
                form = words[1]
            elif words[0] == "element":
                elements.append((words[1], int(words[2]), []))
            elif words[0] == "property" and elements:
                if words[1] == "list":
                    elements[-1][2].append((words[4], PLY_TYPES.get(words[3]), PLY_TYPES.get(words[2])))
                else:
                    elements[-1][2].append((words[2], PLY_TYPES.get(words[1]), None))
            elif words[0] == "end_header":
                break

        if form not in ("ascii", "binary_little_endian", "binary_big_endian"):
            raise ModelError("an unknown PLY format %s" % form)

        order = ">" if form == "binary_big_endian" else "<"
        body = handle.read()

    vertices = []
    faces = []
    tokens = body.split() if form == "ascii" else None
    at = 0

    def take(kind):
        nonlocal at

        if tokens is not None:
            value = tokens[at]
            at += 1
            return float(value) if kind in "fd" else int(value)

        width = struct.calcsize(kind)
        value = struct.unpack_from(order + kind, body, at)[0]
        at += width
        return value

    try:
        for name, count, properties in elements:
            if name == "face":
                check_count(count)

            for _ in range(count):
                values = {}

                for property_name, kind, count_kind in properties:
                    if kind is None:
                        raise ModelError("a PLY property of an unknown type")

                    if count_kind is None:
                        values[property_name] = take(kind)
                    else:
                        values[property_name] = [take(kind) for _ in range(take(count_kind))]

                if name == "vertex":
                    vertices.append((values["x"], values["y"], values["z"]))
                elif name == "face":
                    faces.append(values.get("vertex_indices") or values.get("vertex_index") or [])
    except (IndexError, KeyError, struct.error, ValueError):
        raise ModelError("a broken PLY file")

    soup = []

    try:
        for face in faces:
            for index in range(1, len(face) - 1):
                soup.extend(vertices[face[0]])
                soup.extend(vertices[face[index]])
                soup.extend(vertices[face[index + 1]])
    except IndexError:
        raise ModelError("a PLY face names a corner that the file does not have")

    return soup


# ---------------------------------------------------------------------------
# Preparing


def bounds(soup):
    xs = soup[0::3]
    ys = soup[1::3]
    zs = soup[2::3]

    return (min(xs), min(ys), min(zs)), (max(xs), max(ys), max(zs))


def thin(soup, low, high, grid=GRID):
    """Fewer triangles that look the same at this size: the corners in each cell of a
    fine grid become one corner, at their average, and the triangles that this
    flattens or makes twice are dropped."""
    span = max(high[i] - low[i] for i in range(3)) or 1.0
    step = span / grid
    sums = {}  # cell -> [x, y, z, count]
    kept = []  # the cells of the triangles kept, three per triangle
    seen = set()
    lx, ly, lz = low

    for index in range(0, len(soup), 9):
        corners = []

        for at in (index, index + 3, index + 6):
            x, y, z = soup[at], soup[at + 1], soup[at + 2]
            cell = (int((x - lx) / step), int((y - ly) / step), int((z - lz) / step))
            found = sums.get(cell)

            if found is None:
                sums[cell] = [x, y, z, 1]
            else:
                found[0] += x
                found[1] += y
                found[2] += z
                found[3] += 1

            corners.append(cell)

        a, b, c = corners

        if a == b or b == c or a == c:
            continue

        # The same triangle, whichever corner it starts from, but not turned over.
        first = min(a, b, c)
        signature = (a, b, c) if first == a else (b, c, a) if first == b else (c, a, b)

        if signature in seen:
            continue

        seen.add(signature)
        kept.append(signature)

    points = {cell: (x / n, y / n, z / n) for cell, (x, y, z, n) in sums.items()}
    out = []

    for signature in kept:
        for cell in signature:
            out.extend(points[cell])

    return out


def turn_y_up(soup):
    """A Y-up model, turned so that its Y is up as Z."""
    out = list(soup)
    out[1::3] = [-z for z in soup[2::3]]
    out[2::3] = soup[1::3]
    return out


def signed_volume(soup):
    total = 0.0

    for i in range(0, len(soup), 9):
        ax, ay, az, bx, by, bz, cx, cy, cz = soup[i : i + 9]
        total += ax * (by * cz - bz * cy) - ay * (bx * cz - bz * cx) + az * (bx * cy - by * cx)

    return total / 6.0


def closed_and_consistent(soup):
    """Whether the mesh is closed and its corners all turn the same way: every edge is
    used once in each direction. Only then are the faces turned away from the camera
    surely behind others, and safe to skip."""
    low, high = bounds(soup)
    span = max(high[i] - low[i] for i in range(3)) or 1.0
    grain = span * 1e-6
    directed = {}

    for i in range(0, len(soup), 9):
        keys = [
            (round(soup[j] / grain), round(soup[j + 1] / grain), round(soup[j + 2] / grain)) for j in (i, i + 3, i + 6)
        ]

        for p, q in ((keys[0], keys[1]), (keys[1], keys[2]), (keys[2], keys[0])):
            edge = (p, q)
            directed[edge] = directed.get(edge, 0) + 1

    return all(count == 1 and directed.get((q, p)) == 1 for (p, q), count in directed.items())


def feature_edges(soup, flip):
    """The edges worth a line: creases, open edges, and the pairs of faces that may make
    an outline. Each is (x1, y1, z1, x2, y2, z2, normal, other normal or None, crease)."""
    low, high = bounds(soup)
    span = max(high[i] - low[i] for i in range(3)) or 1.0
    grain = span * 1e-6
    edges = {}
    limit = math.cos(math.radians(CREASE_DEGREES))

    for i in range(0, len(soup), 9):
        a = soup[i : i + 3]
        b = soup[i + 3 : i + 6]
        c = soup[i + 6 : i + 9]
        n = cross((b[0] - a[0], b[1] - a[1], b[2] - a[2]), (c[0] - a[0], c[1] - a[1], c[2] - a[2]))

        if flip:
            n = (-n[0], -n[1], -n[2])

        n = unit(n)

        for p, q in ((a, b), (b, c), (c, a)):
            kp = (round(p[0] / grain), round(p[1] / grain), round(p[2] / grain))
            kq = (round(q[0] / grain), round(q[1] / grain), round(q[2] / grain))

            if kp == kq:
                continue

            key = (kp, kq) if kp < kq else (kq, kp)
            found = edges.get(key)

            if found is None:
                edges[key] = [p, q, n, None, 1]
            else:
                if found[3] is None:
                    found[3] = n
                found[4] += 1

    out = []

    for p, q, n, m, count in edges.values():
        crease = count != 2 or m is None or (n[0] * m[0] + n[1] * m[1] + n[2] * m[2]) < limit
        out.append((p[0], p[1], p[2], q[0], q[1], q[2], n, m, crease))

    return out


# ---------------------------------------------------------------------------
# Drawing


def unit(vector):
    length = math.sqrt(sum(value * value for value in vector)) or 1.0
    return tuple(value / length for value in vector)


def cross(a, b):
    return (a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0])


def iso_rows():
    toward = unit((1.0, -1.0, 1.0))  # the viewer: front right, above
    right = unit(cross((0.0, 0.0, 1.0), toward))
    up = cross(toward, right)
    return (right, up, toward)


# Each view: screen right, screen up, toward the viewer, in model axes (Z up).
VIEWS = [
    ("Front", ((1, 0, 0), (0, 0, 1), (0, -1, 0))),
    ("Right", ((0, 1, 0), (0, 0, 1), (1, 0, 0))),
    ("Top", ((1, 0, 0), (0, 1, 0), (0, 0, 1))),
    ("Iso", iso_rows()),
]

KEY_LIGHT = unit((-0.35, 0.55, 1.0))  # in view axes: from the upper left, in front
FILL_LIGHT = unit((0.6, -0.2, 0.5))


def project(soup, rows):
    """Each corner in view axes: (x, y, depth)."""
    (r0, r1, r2), (u0, u1, u2), (t0, t1, t2) = rows
    xs = soup[0::3]
    ys = soup[1::3]
    zs = soup[2::3]

    return (
        [r0 * x + r1 * y + r2 * z for x, y, z in zip(xs, ys, zs)],
        [u0 * x + u1 * y + u2 * z for x, y, z in zip(xs, ys, zs)],
        [t0 * x + t1 * y + t2 * z for x, y, z in zip(xs, ys, zs)],
    )


def extent(points):
    xs, ys, _ = points
    return min(xs), max(xs), min(ys), max(ys)


def draw_view(points, flip, scale, centre, size, rows, edges, depth_range, shade_depth, two_sided=False):
    """One view as an image `size` pixels square, drawn SCALE times larger."""
    xs, ys, ds = points
    near, far = depth_range
    deep = (near - far) or 1.0
    big = size * SCALE
    image = Image.new("RGB", (big, big), TILE_BACKGROUND)
    draw = ImageDraw.Draw(image)
    s = scale * SCALE
    cx, cy = centre
    half = big / 2.0
    pieces = []  # (depth, colour, polygon)
    lx, ly, lz = KEY_LIGHT
    fx, fy, fz = FILL_LIGHT

    for i in range(0, len(xs), 3):
        ax, ay, az = xs[i], ys[i], ds[i]
        bx, by, bz = xs[i + 1], ys[i + 1], ds[i + 1]
        qx, qy, qz = xs[i + 2], ys[i + 2], ds[i + 2]

        # The face's normal in view axes: facing away, it is behind the model.
        ux, uy, uz = bx - ax, by - ay, bz - az
        vx, vy, vz = qx - ax, qy - ay, qz - az
        nx = uy * vz - uz * vy
        ny = uz * vx - ux * vz
        nz = ux * vy - uy * vx

        if flip:
            nx, ny, nz = -nx, -ny, -nz

        if nz <= 0:
            # An open or muddled mesh: this face may be all there is to see, so it is
            # drawn, lit as if it faced the camera. Further back, it is drawn first.
            if not two_sided:
                continue

            nx, ny, nz = -nx, -ny, -nz

        length = math.sqrt(nx * nx + ny * ny + nz * nz) or 1.0
        nx, ny, nz = nx / length, ny / length, nz / length
        light = 0.28 + 0.62 * max(0.0, nx * lx + ny * ly + nz * lz) + 0.18 * max(0.0, nx * fx + ny * fy + nz * fz)
        light = min(light, 1.0)

        if shade_depth:
            # A flat view lights a face the same at any depth: what is further back is darker.
            light *= 0.5 + 0.5 * ((az + bz + qz) / 3 - far) / deep

        colour = (int(MODEL[0] * light), int(MODEL[1] * light), int(MODEL[2] * light))

        # On screen: x right, y down.
        corners = [
            (half + (ax - cx) * s, half - (ay - cy) * s, az),
            (half + (bx - cx) * s, half - (by - cy) * s, bz),
            (half + (qx - cx) * s, half - (qy - cy) * s, qz),
        ]
        stack = [corners]

        while stack:
            triangle = stack.pop()
            (x1, y1, d1), (x2, y2, d2), (x3, y3, d3) = triangle

            if max(x1, x2, x3) - min(x1, x2, x3) > SPLIT_PIXELS or max(y1, y2, y3) - min(y1, y2, y3) > SPLIT_PIXELS:
                m12 = ((x1 + x2) / 2, (y1 + y2) / 2, (d1 + d2) / 2)
                m23 = ((x2 + x3) / 2, (y2 + y3) / 2, (d2 + d3) / 2)
                m31 = ((x3 + x1) / 2, (y3 + y1) / 2, (d3 + d1) / 2)
                stack.extend(
                    (
                        (triangle[0], m12, m31),
                        (m12, triangle[1], m23),
                        (m31, m23, triangle[2]),
                        (m12, m23, m31),
                    )
                )
                continue

            pieces.append((d1 + d2 + d3, colour, ((x1, y1), (x2, y2), (x3, y3))))

    # The edge lines: creases, and outlines (one face toward the viewer, one away).
    # Each sits a hair in front of its faces and is cut like them, so a face nearer
    # the viewer still hides it.
    (r0, r1, r2), (u0, u1, u2), (t0, t1, t2) = rows
    lift = deep * 0.015

    for x1, y1, z1, x2, y2, z2, n, m, crease in edges:
        front = n[0] * t0 + n[1] * t1 + n[2] * t2 > 0

        if m is None:
            if not front:
                continue
        else:
            other = m[0] * t0 + m[1] * t1 + m[2] * t2 > 0

            if not (front or other) or (not crease and front == other):
                continue

        sx1 = half + (r0 * x1 + r1 * y1 + r2 * z1 - cx) * s
        sy1 = half - (u0 * x1 + u1 * y1 + u2 * z1 - cy) * s
        sd1 = t0 * x1 + t1 * y1 + t2 * z1
        sx2 = half + (r0 * x2 + r1 * y2 + r2 * z2 - cx) * s
        sy2 = half - (u0 * x2 + u1 * y2 + u2 * z2 - cy) * s
        sd2 = t0 * x2 + t1 * y2 + t2 * z2
        steps = max(1, int(max(abs(sx2 - sx1), abs(sy2 - sy1)) / SPLIT_PIXELS) + 1)

        for step in range(steps):
            f1 = step / steps
            f2 = (step + 1) / steps
            px1, py1 = sx1 + (sx2 - sx1) * f1, sy1 + (sy2 - sy1) * f1
            px2, py2 = sx1 + (sx2 - sx1) * f2, sy1 + (sy2 - sy1) * f2
            depth = sd1 + (sd2 - sd1) * (f1 + f2) / 2
            pieces.append((3 * (depth + lift), None, ((px1, py1), (px2, py2))))

    pieces.sort(key=lambda piece: piece[0])
    polygon = draw.polygon
    line = draw.line
    width = max(1, SCALE)

    for _, colour, corners in pieces:
        if colour is None:
            line(corners, fill=EDGE, width=width)
        else:
            # The outline too, in the same colour: no hairline gaps between neighbours.
            polygon(corners, fill=colour, outline=colour)

    return image.resize((size, size), Image.LANCZOS)


def font(size):
    try:
        return ImageFont.load_default(size=size)
    except TypeError:  # Pillow before 10.1: one size only
        return ImageFont.load_default()


def number(value):
    return ("%.1f" % value).rstrip("0").rstrip(".")


def render_views(path, out_path, up="z", tile=TILE):
    """Draw the four views of the model at `path` into a PNG at `out_path`. Returns what was drawn."""
    soup = read_model(path)
    read_triangles = len(soup) // 9

    if up == "y":
        soup = turn_y_up(soup)

    low, high = bounds(soup)
    size = tuple(high[i] - low[i] for i in range(3))

    if max(size) <= 0:
        raise ModelError("the model has no size")

    # A dense mesh (a scan) keeps most of its triangles at the first grid: a coarser
    # grid each time, sized from how far over it still is.
    grid = GRID

    while len(soup) // 9 > DRAW_TRIANGLES:
        soup = thin(soup, low, high, grid)

        if grid <= MIN_GRID:
            break

        grid = max(MIN_GRID, int(grid * math.sqrt(DRAW_TRIANGLES / max(1, len(soup) // 9)) * 0.9))

    # A mesh turned inside out (corners clockwise): its normals point in.
    flip = signed_volume(soup) < 0
    # Faces turned away are skipped only on a closed mesh whose corners agree.
    two_sided = not closed_and_consistent(soup)

    projected = [(name, project(soup, rows)) for name, rows in VIEWS]
    # A thinned mesh (a scan, a sculpt) is bumpy at the grid's scale, and every bump
    # would draw a line: shading alone draws it.
    edges = [] if len(soup) // 9 < read_triangles else feature_edges(soup, flip)
    room = tile * (1 - 2 * MARGIN)

    # The three flat views share one scale, so the part keeps its size between them.
    flat_scale = min(
        room / max(right - left, top - bottom, 1e-9) for _, points in projected[:3] for left, right, bottom, top in [extent(points)]
    )

    caption_height = 34
    sheet = Image.new("RGB", (tile * 2 + 3, tile * 2 + 3 + caption_height), BACKGROUND)
    label = font(16)

    for index, (name, points) in enumerate(projected):
        left, right, bottom, top = extent(points)
        scale = flat_scale if index < 3 else room / max(right - left, top - bottom, 1e-9)
        depths = points[2]
        view = draw_view(
            points,
            flip,
            scale,
            ((left + right) / 2, (bottom + top) / 2),
            tile,
            VIEWS[index][1],
            edges,
            (max(depths), min(depths)),
            index < 3,
            two_sided,
        )
        ImageDraw.Draw(view).text((10, 8), name, fill=TEXT_DIM, font=label)
        sheet.paste(view, (1 + (index % 2) * (tile + 1), 1 + (index // 2) * (tile + 1)))

    draw = ImageDraw.Draw(sheet)
    draw.rectangle((0, 0, sheet.width - 1, tile * 2 + 2), outline=LINE)
    draw.line((tile + 1, 0, tile + 1, tile * 2 + 2), fill=LINE)
    draw.line((0, tile + 1, sheet.width - 1, tile + 1), fill=LINE)

    # STL and 3MF are in millimetres, as slicers read them; OBJ and PLY say nothing.
    unit_name = " mm" if os.path.splitext(path)[1].lower() in (".stl", ".3mf") else ""
    caption = "%s  \u00b7  %s x %s x %s%s  \u00b7  %s triangles" % (
        os.path.basename(path),
        number(size[0]),
        number(size[1]),
        number(size[2]),
        unit_name,
        format(read_triangles, ","),
    )
    draw.text((10, tile * 2 + 3 + 8), caption, fill=TEXT, font=font(17))
    sheet.save(out_path, "PNG", optimize=False)

    return {"triangles": read_triangles, "size": [round(value, 3) for value in size]}
