# Scripted *illustrative* conversation used to render docs/*.png and docs/demo.gif.
# Usage: MOCK_FONT_DIR=file:///path/to/fonts python3 render.py scenario ../   (needs Chrome + ffmpeg + Pillow)
from render import *
SPEC=dict(name='Shop Bot', icon='🛍', c1='#7a5af8', c2='#1fb6c9', scenarios=[
 dict(steps=[
  U('/start'),
  B('🛍 به فروشگاه خوش آمدی!\nمحصولات را ببین، سفارشت را پیگیری کن یا با پشتیبانی صحبت کن.', id='m',
    buttons=[['🛒 محصولات','📦 پیگیری سفارش'],['💬 پشتیبانی','🌐 English']]),
  dict(press='m', label='🛒 محصولات'),
  B('🛒 <b>دسته‌بندی‌ها</b> (از API سایت)', id='c',
    buttons=[['🎧 صوتی','⌚ ساعت هوشمند'],['🔌 لوازم جانبی','🔥 پرفروش‌ها']]),
 ]),
 dict(steps=[
  B(media=image('#7a5af8','#1fb6c9','🎧','نمونه محصول'), id='p',
    text='🎧 <b>هدفون بی‌سیم (نمونه)</b>\n💰 قیمت: <b>۱٬۲۰۰٬۰۰۰ تومان</b>\n✅ موجود در انبار\n\n<i>داده‌ها نمونه‌اند.</i>',
    buttons=[['🛒 مشاهده در سایت'],['⬅️ بازگشت','➡️ بعدی']]),
 ]),
 dict(steps=[
  U('📦 پیگیری سفارش'),
  B('شمارهٔ سفارش را بفرست:', id='a'),
  U('<code>#10452</code>'),
  B('📦 <b>سفارش #10452</b>\n\n🟢 پرداخت‌شده\n🟢 آماده‌سازی\n🟡 <b>ارسال‌شده</b> — کد رهگیری: <code>0000-EXAMPLE</code>\n⚪ تحویل', id='o',
    buttons=[['💬 تماس با پشتیبانی']]),
 ]),
])
