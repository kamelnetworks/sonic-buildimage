# Centec SAI

export CENTEC_SAI_VERSION = 1.11.0-2
export CENTEC_SAI = libsaictc_$(CENTEC_SAI_VERSION)_$(PLATFORM_ARCH).deb
export CENTEC_SAI_DEV = libsaictc-dev_$(CENTEC_SAI_VERSION)_$(PLATFORM_ARCH).deb

$(CENTEC_SAI)_URL = https://github.com/CentecNetworks/sonic-binaries/raw/master/$(PLATFORM_ARCH)/sai/$(CENTEC_SAI)
$(CENTEC_SAI_DEV)_URL = https://github.com/CentecNetworks/sonic-binaries/raw/master/$(PLATFORM_ARCH)/sai/$(CENTEC_SAI_DEV)
$(eval $(call add_conflict_package,$(CENTEC_SAI_DEV),$(LIBSAIVS_DEV)))

SONIC_ONLINE_DEBS += $(CENTEC_SAI)
SONIC_ONLINE_DEBS += $(CENTEC_SAI_DEV)

