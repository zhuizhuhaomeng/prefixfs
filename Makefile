KERNEL ?= /boot/vmlinuz-$(shell uname -r)
QEMU ?=
KDIR ?= /lib/modules/$(shell uname -r)/build

.PHONY: all clean test vm-test
all:
	$(MAKE) -C $(KDIR) M=$(CURDIR) W=1 modules

clean:
	$(MAKE) -C $(KDIR) M=$(CURDIR) clean

test: vm-test
vm-test: all
	python3 tools/vm-test.py --kernel "$(KERNEL)" $(if $(QEMU),--qemu "$(QEMU)")
