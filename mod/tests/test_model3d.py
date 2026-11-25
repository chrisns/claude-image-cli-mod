"""bin/model3d.py and render.py's `model` command: reading each format, and drawing."""

import json
import os
import struct
import subprocess
import sys
import tempfile
import unittest
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
BIN = os.path.join(HERE, "..", "bin")
sys.path.insert(0, BIN)

try:
    import model3d
    from PIL import Image

    HAVE_PILLOW = True
except ImportError:  # the ImageMagick job has no Pillow
    HAVE_PILLOW = False


def box(x0, y0, z0, x1, y1, z1):
    """The 12 triangles of a box, corners counter-clockwise seen from outside."""
    p = [(x, y, z) for z in (z0, z1) for y in (y0, y1) for x in (x0, x1)]
    faces = [
        (0, 2, 3, 1),  # bottom
        (4, 5, 7, 6),  # top
        (0, 1, 5, 4),  # front
        (2, 6, 7, 3),  # back
        (0, 4, 6, 2),  # left
        (1, 3, 7, 5),  # right
    ]
    triangles = []

    for a, b, c, d in faces:
        triangles.append((p[a], p[b], p[c]))
        triangles.append((p[a], p[c], p[d]))

    return triangles


def binary_stl(triangles, header=b"made by a test"):
    data = header.ljust(80, b" ")[:80] + struct.pack("<I", len(triangles))

    for a, b, c in triangles:
        data += struct.pack("<12fH", 0, 0, 0, *a, *b, *c, 0)

    return data


def ascii_stl(triangles):
    lines = ["solid test"]

    for triangle in triangles:
        lines.append("facet normal 0 0 0\nouter loop")
        lines.extend("vertex %g %g %g" % corner for corner in triangle)
        lines.append("endloop\nendfacet")

    lines.append("endsolid test")
    return "\n".join(lines)


@unittest.skipUnless(HAVE_PILLOW, "3D models need Pillow")
class Reading(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.folder.cleanup()

    def write(self, name, data):
        path = os.path.join(self.folder.name, name)

        with open(path, "wb" if isinstance(data, bytes) else "w") as handle:
            handle.write(data)

        return path

    def test_binary_stl(self):
        soup = model3d.read_model(self.write("a.stl", binary_stl(box(0, 0, 0, 1, 2, 3))))
        self.assertEqual(len(soup), 12 * 9)
        self.assertEqual(model3d.bounds(soup), ((0, 0, 0), (1, 2, 3)))

    def test_binary_stl_whose_header_says_solid(self):
        # Many exporters write "solid" in a binary header: the size says it is binary.
        soup = model3d.read_model(self.write("a.stl", binary_stl(box(0, 0, 0, 1, 1, 1), header=b"solid exported")))
        self.assertEqual(len(soup), 12 * 9)

    def test_ascii_stl(self):
        soup = model3d.read_model(self.write("a.stl", ascii_stl(box(0, 0, 0, 4, 5, 6))))
        self.assertEqual(model3d.bounds(soup), ((0, 0, 0), (4, 5, 6)))

    def test_obj_with_a_quad_and_negative_indices(self):
        text = "v 0 0 0\nv 1 0 0\nv 1 1 0\nv 0 1 0\nvn 0 0 1\nf 1//1 2//1 3//1 4//1\nf -4 -3 -2\n"
        soup = model3d.read_model(self.write("a.obj", text))
        self.assertEqual(len(soup), 3 * 9)  # the quad is two triangles

    def test_3mf_with_a_component_and_transforms(self):
        mesh = "".join('<vertex x="%g" y="%g" z="%g"/>' % corner for triangle in box(0, 0, 0, 1, 1, 1) for corner in triangle)
        triangles = "".join('<triangle v1="%d" v2="%d" v3="%d"/>' % (i, i + 1, i + 2) for i in range(0, 36, 3))
        model = (
            '<?xml version="1.0"?><model xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02" unit="millimeter">'
            "<resources>"
            '<object id="1" type="model"><mesh><vertices>%s</vertices><triangles>%s</triangles></mesh></object>'
            '<object id="2" type="model"><components><component objectid="1" transform="2 0 0 0 2 0 0 0 2 0 0 0"/></components></object>'
            "</resources>"
            '<build><item objectid="2" transform="1 0 0 0 1 0 0 0 1 10 20 0"/></build></model>'
        ) % (mesh, triangles)
        path = os.path.join(self.folder.name, "a.3mf")

        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("3D/3dmodel.model", model)

        soup = model3d.read_model(path)
        # Scaled by two, then moved by (10, 20, 0).
        self.assertEqual(model3d.bounds(soup), ((10, 20, 0), (12, 22, 2)))

    def test_ascii_ply(self):
        text = (
            "ply\nformat ascii 1.0\nelement vertex 4\nproperty float x\nproperty float y\nproperty float z\n"
            "element face 1\nproperty list uchar int vertex_indices\nend_header\n"
            "0 0 0\n1 0 0\n1 1 0\n0 1 0\n4 0 1 2 3\n"
        )
        soup = model3d.read_model(self.write("a.ply", text))
        self.assertEqual(len(soup), 2 * 9)

    def test_refusals(self):
        with self.assertRaisesRegex(model3d.ModelError, "not a model file"):
            model3d.read_model(self.write("a.txt", "hello"))

        with self.assertRaisesRegex(model3d.ModelError, "empty"):
            model3d.read_model(self.write("b.stl", ""))

        with self.assertRaisesRegex(model3d.ModelError, "not a zip"):
            model3d.read_model(self.write("c.3mf", "not a zip"))

        with self.assertRaisesRegex(model3d.ModelError, "names a corner"):
            model3d.read_model(self.write("d.obj", "v 0 0 0\nf 1 2 3\n"))


@unittest.skipUnless(HAVE_PILLOW, "3D models need Pillow")
class Drawing(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.folder.cleanup()

    def test_four_views_in_one_picture(self):
        model = os.path.join(self.folder.name, "cube.stl")

        with open(model, "wb") as handle:
            handle.write(binary_stl(box(0, 0, 0, 20, 20, 10)))

        out = os.path.join(self.folder.name, "views.png")
        facts = model3d.render_views(model, out, tile=200)

        self.assertEqual(facts, {"triangles": 12, "size": [20, 20, 10]})

        with Image.open(out) as picture:
            self.assertEqual(picture.size[0], 2 * 200 + 3)

    def test_a_small_boss_on_a_big_plate_stays_in_front(self):
        # Sorted as whole triangles, the plate's big top face would cover the boss.
        model = os.path.join(self.folder.name, "plate.stl")

        with open(model, "wb") as handle:
            handle.write(binary_stl(box(0, 0, 0, 100, 100, 4) + box(45, 45, 4, 55, 55, 30)))

        out = os.path.join(self.folder.name, "views.png")
        tile = 200
        model3d.render_views(model, out, tile=tile)

        with Image.open(out) as picture:
            picture = picture.convert("RGB")
            # The top view is the lower left tile: its middle is the boss's top face,
            # nearer than the plate, so lighter in a flat view.
            top_left = (1, tile + 2)
            boss = picture.getpixel((top_left[0] + tile // 2, top_left[1] + tile // 2))
            plate = picture.getpixel((top_left[0] + tile // 2, top_left[1] + tile // 2 + 40))

        self.assertGreater(sum(boss), sum(plate))

    def test_a_dense_mesh_is_thinned_before_drawing(self):
        triangles = []

        for i in range(300):
            for j in range(300):
                triangles.append(((i, j, 0), (i + 1, j, 0), (i + 1, j + 1, 0)))
                triangles.append(((i, j, 0), (i + 1, j + 1, 0), (i, j + 1, 0)))

        soup = [value for triangle in triangles for corner in triangle for value in corner]
        low, high = model3d.bounds(soup)
        thinned = model3d.thin(soup, low, high, grid=50)

        self.assertLess(len(thinned), len(soup) // 10)


@unittest.skipUnless(HAVE_PILLOW, "3D models need Pillow")
class Command(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.env = dict(os.environ, INLINE_IMAGES_CACHE=os.path.join(self.folder.name, "cache"))
        self.model = os.path.join(self.folder.name, "part.stl")

        with open(self.model, "wb") as handle:
            handle.write(binary_stl(box(0, 0, 0, 45, 10, 6)))

    def tearDown(self):
        self.folder.cleanup()

    def run_helper(self, *arguments):
        ran = subprocess.run(
            [sys.executable, os.path.join(BIN, "render.py"), *arguments], capture_output=True, text=True, env=self.env
        )
        return json.loads(ran.stdout)

    def test_draws_stores_and_reports(self):
        reply = self.run_helper("model", self.model)

        self.assertTrue(reply["ok"], reply)
        self.assertEqual(reply["format"], "PNG")
        self.assertEqual(reply["triangles"], 12)
        self.assertEqual(reply["size"], [45, 10, 6])
        self.assertTrue(reply["named"].endswith("part views.png"))
        self.assertNotIn("data", reply)

    def test_data_and_the_cache(self):
        first = self.run_helper("model", self.model, "--data")
        again = self.run_helper("model", self.model)

        self.assertTrue(first["data"].startswith("iVBORw0KGgo"))  # a PNG
        self.assertEqual(first["path"], again["path"])

    def test_a_changed_file_is_drawn_again(self):
        first = self.run_helper("model", self.model)

        with open(self.model, "wb") as handle:
            handle.write(binary_stl(box(0, 0, 0, 5, 5, 50)))

        os.utime(self.model, (1, 1))  # a different time, even within one clock tick
        again = self.run_helper("model", self.model)

        self.assertNotEqual(first["path"], again["path"])
        self.assertEqual(again["size"], [5, 5, 50])

    def test_failures(self):
        self.assertIn("cannot read", self.run_helper("model", os.path.join(self.folder.name, "gone.stl"))["error"])
        text = os.path.join(self.folder.name, "notes.txt")

        with open(text, "w") as handle:
            handle.write("hello")

        self.assertIn("not a model file", self.run_helper("model", text)["error"])


if __name__ == "__main__":
    unittest.main()
