import os
import sys

try:
    from PIL import Image
except ImportError:
    # Auto-install Pillow if not present
    import subprocess
    print("Pillow not found, installing via pip...")
    subprocess.check_call([sys.executable, "-m", "pip", "install", "Pillow"])
    from PIL import Image

def generate_standard_jpeg(filename, color_rgb):
    # Generate a proper, fully-compliant 224x224 JPEG
    os.makedirs(os.path.dirname(filename), exist_ok=True)
    img = Image.new("RGB", (224, 224), color_rgb)
    img.save(filename, "JPEG")
    print(f"Generated standard compliant JPEG: {filename} ({color_rgb})")

if __name__ == "__main__":
    # Red representing animal (raccoon) trigger
    generate_standard_jpeg("tests/fixtures/raccoon.jpg", (255, 0, 0))
    # Green representing empty garden
    generate_standard_jpeg("tests/fixtures/empty_garden.jpg", (0, 255, 0))
