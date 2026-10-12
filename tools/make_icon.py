from pathlib import Path
from PIL import Image,ImageDraw

root=Path(__file__).resolve().parent.parent
image=Image.new("RGBA",(256,256),(0,0,0,0))
draw=ImageDraw.Draw(image)
draw.rounded_rectangle((8,8,248,248),radius=52,fill="#101722")
draw.polygon([(45,68),(111,81),(128,98),(128,203),(111,186),(45,173)],fill="#56d6b5")
draw.polygon([(211,68),(145,81),(128,98),(128,203),(145,186),(211,173)],fill="#dceee7")
draw.line([(128,101),(128,195)],fill="#101722",width=8)
for y in [105,127,149]:
    draw.line([(61,y),(101,y+8)],fill="#17392f",width=6)
    draw.line([(155,y+8),(195,y)],fill="#648f81",width=6)
(root/"assets").mkdir(exist_ok=True)
image.save(root/"assets"/"icon.ico",sizes=[(16,16),(32,32),(48,48),(64,64),(128,128),(256,256)])
