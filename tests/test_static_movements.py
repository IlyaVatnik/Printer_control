import numpy as np
from Printer_control.Printer import Printer, PrinterConfig

'''
Если насадка выступает вправо по X на 30 мм и влево на 5 мм:
attach_min_x = -5, attach_max_x = +30

Если колесо ниже сопла на 12 мм (выступ вниз, т.е. к столу), и вверх насадка не выступает:
attach_min_z = -12, attach_max_z = 0

Если вперёд по Y выступ 20 мм, назад 0:
attach_min_y = 0, attach_max_y = +20
'''

p = Printer(PrinterConfig(
    base_url="http://10.2.15.109:7125",
    attach_min_x=-15,  attach_max_x=15,
    attach_min_y=-5,  attach_max_y=0,
    attach_min_z=-94, attach_max_z=0,
    ))

print(p.printer_info())


p.home('XYZ')
velocity_mm_s=100
accel_mm_s2=500
p.set_motion_limits(velocity_mm_s=velocity_mm_s, accel_mm_s2=accel_mm_s2)
#%%
z_safe=100
z_contact=96


for x in np.arange(300,380,10):
    for y in np.arange(300,400,10):
        p.safe_y_pass(x=x, y_start=y, y_end=y, z_safe=z_safe, z_contact=z_safe)
        p.move_absolute(x=x, y=y, z=z_contact, speed_mm_s=velocity_mm_s)
        p.move_absolute(x=x, y=y, z=z_safe, speed_mm_s=velocity_mm_s)
        