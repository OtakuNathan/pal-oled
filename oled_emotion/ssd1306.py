import os
import fcntl
from PIL import Image, ImageDraw, ImageFont

I2C_SLAVE = 0x0703


class RawI2C:
    def __init__(self, bus=1, addr=0x3C):
        self.addr = addr
        self.fd = os.open(f"/dev/i2c-{bus}", os.O_RDWR)
        fcntl.ioctl(self.fd, I2C_SLAVE, addr)

    def write_cmd(self, *cmds):
        os.write(self.fd, bytes([0x00] + list(cmds)))

    def write_data(self, data):
        """分块写入数据，每块带 0x40 控制字节，避免内核拆消息导致控制字节丢失"""
        CHUNK = 128  # 每块128字节数据
        d = bytes(data)
        for i in range(0, len(d), CHUNK):
            os.write(self.fd, b'\x40' + d[i:i + CHUNK])

    def close(self):
        os.close(self.fd)


class SSD1306:
    ADDR = 0x3C
    WIDTH = 128
    HEIGHT = 64

    def __init__(self, bus_num=1):
        self.i2c = RawI2C(bus=bus_num, addr=self.ADDR)
        self.font_sm = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf", 10)
        self.font_md = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf", 14)
        self.font_lg = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf", 20)
        self._init()

    def _cmd(self, *cmds):
        self.i2c.write_cmd(*cmds)

    def _init(self):
        self._cmd(0xAE)
        self._cmd(0xD5, 0x80)
        self._cmd(0xA8, 0x3F)
        self._cmd(0xD3, 0x00)
        self._cmd(0x40)
        self._cmd(0x8D, 0x14)
        self._cmd(0x20, 0x00)  # 水平地址模式，地址自动跨页递增
        self._cmd(0xA1)
        self._cmd(0xC8)
        self._cmd(0xDA, 0x12)
        self._cmd(0x81, 0xFF)
        self._cmd(0xD9, 0xF1)
        self._cmd(0xDB, 0x40)
        self._cmd(0xA4)
        self._cmd(0xA6)
        self._cmd(0xAF)

    def _send_image(self, img):
        buf = bytearray(128 * 8)
        pixels = img.load()
        for page in range(8):
            for x in range(128):
                byte = 0
                for bit in range(8):
                    y = page * 8 + bit
                    if pixels[x, y]:
                        byte |= (1 << bit)
                buf[x + page * 128] = byte
        # 设置写入范围后连续写入，水平模式地址自动递增
        self._cmd(0x21, 0, 127)
        self._cmd(0x22, 0, 7)
        self.i2c.write_data(buf)

    def clear(self):
        img = Image.new("1", (128, 64), 0)
        self._send_image(img)

    def show(self, text_lines, size='sm'):
        font_map = {'sm': self.font_sm, 'md': self.font_md, 'lg': self.font_lg}
        font = font_map.get(size, self.font_sm)
        img = Image.new("1", (128, 64), 0)
        draw = ImageDraw.Draw(img)
        y = 2
        for line in text_lines:
            draw.text((2, y), line, font=font, fill=1)
            y += font.getbbox('M')[3] + 2
        self._send_image(img)

    def show_image(self, img):
        if img.size != (128, 64):
            img = img.resize((128, 64))
        if img.mode != "1":
            img = img.convert("1")
        self._send_image(img)

    def off(self):
        self._cmd(0xAE)

    def on(self):
        self._cmd(0x8D, 0x14, 0xAF)

    def close(self):
        self.i2c.close()

    def show_gif(self, gif_path, loop=True, callback=None):
        import time
        from PIL import Image as PILImage
        img = PILImage.open(gif_path)
        total = img.n_frames
        bg = PILImage.new("RGBA", img.size, (0, 0, 0, 0))
        try:
            while True:
                for i in range(total):
                    img.seek(i)
                    frame_rgba = img.convert("RGBA")
                    disposal = img.info.get("disposal", 0)
                    if disposal == 2:
                        bg = PILImage.new("RGBA", img.size, (0, 0, 0, 0))
                    composite = PILImage.alpha_composite(bg, frame_rgba)
                    bg = composite.copy()
                    frame_out = composite.convert("1", dither=Image.NONE).resize((self.WIDTH, self.HEIGHT))
                    self._send_image(frame_out)
                    duration = img.info.get("duration", 50) / 1000.0
                    if callback and not callback(i, total):
                        return
                    time.sleep(duration)
                if not loop:
                    break
        except KeyboardInterrupt:
            pass
        finally:
            self.clear()
