# Manufacturer prospect research — ranked records

Research date: 2026-10-21. Generated from [manufacturers.csv](manufacturers.csv); see [methodology and discovery plan](README.md). No outreach occurred.

50 unique companies: 28 industrial, 14 energy, 8 connected commercial equipment. Rankings are research hypotheses, not sales qualification.

Score = 2 × Linux evidence + application boundary − friction; ties use company name. All fleet sizes, budgets and buying interest are unknown. Documentation does not prove current stock or deployment.

## 1. Advantech — UNO-420

**industrial; manufacturer/channel. Score 5 (Linux 2, boundary 2, friction 1).

**Documented:** UNO-420 manual documents Ubuntu Linux or Windows OS modes. [Primary source](https://advdownload.advantech.com/productfile/Downloadfile4/1-1XS5WFT/UNO-420_EN_User_Manual_Ed.2.PDF). Source basis: Vendor documentation.

**OTA evidence:** Unknown.

**Inferred pilot:** Linux serial-data application deployment. **Inferred migration/sales/safety friction:** SKU/image compatibility and downstream integrator ownership.

**Geography:** Unknown; not established by cited source. **Availability caveat:** Public documentation found; current orderability, support term and installed configuration unverified.

## 2. Compulab — IOT-GATE-RPI4

**industrial; manufacturer/channel. Score 5 (Linux 2, boundary 2, friction 1).

**Documented:** Industrial Raspberry Pi gateway has published Linux documentation. [Primary source](https://www.compulab.com/products/iot-gateways/iot-gate-rpi4-industrial-raspberry-pi-iot-gateway/). Source basis: Vendor documentation.

**OTA evidence:** Unknown.

**Inferred pilot:** Debian gateway component update with persistent state. **Inferred migration/sales/safety friction:** Older RPi generation; confirm SKU lifecycle and OEM software ownership.

**Geography:** Unknown; not established by cited source. **Availability caveat:** Public documentation found; current orderability, support term and installed configuration unverified.

## 3. embeddedTS (Technologic Systems) — TS-7800-V2

**industrial; manufacturer/channel. Score 5 (Linux 2, boundary 2, friction 1).

**Documented:** TS-7800-V2 manual documents Debian Linux images and BSP/kernel support. [Primary source](https://docs.embeddedts.com/TS-7800-V2). Source basis: Vendor documentation.

**OTA evidence:** Unknown.

**Inferred pilot:** Non-control data collection application on supported Debian image. **Inferred migration/sales/safety friction:** Armv7 ABI, legacy image migration and end-OEM ownership; confirm current shipping image.

**Geography:** Unknown; not established by cited source. **Availability caveat:** Public documentation found; current orderability, support term and installed configuration unverified.

## 4. InHand Networks — IG502

**industrial; manufacturer/platform vendor. Score 5 (Linux 2, boundary 2, friction 1).

**Documented:** IG502 product documentation specifies custom Linux and industrial protocol support. [Primary source](https://www.inhand.com/en/products/edge-gateways/ig502/). Source basis: Vendor documentation.

**OTA evidence:** Unknown.

**Inferred pilot:** Read-only protocol collector bundle. **Inferred migration/sales/safety friction:** Custom Linux ABI and privileged access; vendor management integration.

**Geography:** Unknown; not established by cited source. **Availability caveat:** Public documentation found; current orderability, support term and installed configuration unverified.

## 5. Kontron — KBox A-230-LS

**industrial; manufacturer/channel. Score 5 (Linux 2, boundary 2, friction 1).

**Documented:** KBox manual specifies a Linux Yocto operating system. [Primary source](https://www.kontron.com/downloads/manuals/k/kbox-a-230-ls_user-guide_rev-preliminary_0-3_2020-07-22.pdf?product=155778). Source basis: Vendor documentation.

**OTA evidence:** Unknown.

**Inferred pilot:** Yocto monitoring package in lab image. **Inferred migration/sales/safety friction:** Preliminary older manual; verify current SKU/BSP and support ownership.

**Geography:** Unknown; not established by cited source. **Availability caveat:** Older/preliminary technical evidence; reconfirm current product, ownership, orderability and support before discovery.

## 6. KUNBUS — Revolution Pi

**industrial; manufacturer/channel. Score 5 (Linux 2, boundary 2, friction 1).

**Documented:** Vendor documents customized Raspberry Pi OS images and its package repositories. [Primary source](https://revolutionpi.com/en/docs/revpi-images). Source basis: Vendor documentation.

**OTA evidence:** Vendor OS/package repositories documented; fleet OTA provider unknown.

**Inferred pilot:** Read-only telemetry collector deployment. **Inferred migration/sales/safety friction:** Industrial control boundary; downstream integrators own applications.

**Geography:** Unknown; not established by cited source. **Availability caveat:** Public documentation found; current orderability, support term and installed configuration unverified.

## 7. OnLogic — ML100G industrial computers

**industrial; manufacturer/channel. Score 5 (Linux 2, boundary 2, friction 1).

**Documented:** Official guide describes Greengrass setup on industrial computers with Ubuntu options. [Primary source](https://support.onlogic.com/wp-content/uploads/2020/06/Getting-Started-with-AWS-IoT-Greengrass-Updated-4-24-25.pdf). Source basis: Vendor documentation.

**OTA evidence:** Greengrass integration documented; installed provider unknown.

**Inferred pilot:** Customer-selected Linux gateway application distribution. **Inferred migration/sales/safety friction:** OEM/integrator may own software; enterprise qualification.

**Geography:** Unknown; not established by cited source. **Availability caveat:** Public documentation found; current orderability, support term and installed configuration unverified.

## 8. PHYTEC — Embedded Linux module platforms

**industrial; manufacturer/channel. Score 5 (Linux 2, boundary 2, friction 1).

**Documented:** Vendor security guide describes RAUC in its Linux distribution. [Primary source](https://www.phytec.de/fileadmin/phytec_base/pdfs/Embedded-Security_2024_E_web.pdf). Source basis: Vendor documentation.

**OTA evidence:** RAUC platform update integration documented; fleet backend unknown. [Source](https://www.phytec.de/fileadmin/phytec_base/pdfs/Embedded-Security_2024_E_web.pdf).

**Inferred pilot:** Authenticated application delivery alongside platform RAUC. **Inferred migration/sales/safety friction:** OEM owns end product; preserve RAUC and boot trust configuration.

**Geography:** Unknown; not established by cited source. **Availability caveat:** Public documentation found; current orderability, support term and installed configuration unverified.

## 9. Seeed Studio — reComputer Industrial

**industrial; manufacturer/channel. Score 5 (Linux 2, boundary 2, friction 1).

**Documented:** Industrial Jetson systems ship with JetPack/Linux support. [Primary source](https://wiki.seeedstudio.com/reComputer_Industrial_Getting_Started/). Source basis: Vendor documentation.

**OTA evidence:** Unknown for this product; do not generalize balena partnerships to installed fleets.

**Inferred pilot:** Non-safety vision application bundle on a lab gateway. **Inferred migration/sales/safety friction:** JetPack/BSP coupling; downstream OEM controls deployment.

**Geography:** Unknown; not established by cited source. **Availability caveat:** Public documentation found; current orderability, support term and installed configuration unverified.

## 10. Sfera Labs — Strato Pi

**industrial; manufacturer/channel. Score 5 (Linux 2, boundary 2, friction 1).

**Documented:** Vendor provides Strato Pi kernel driver source and DKMS installation instructions. [Primary source](https://github.com/sfera-labs/strato-pi-kernel-module). Source basis: Vendor documentation.

**OTA evidence:** Unknown.

**Inferred pilot:** Non-control monitoring agent on industrial Pi. **Inferred migration/sales/safety friction:** Kernel module/image compatibility; building-control use needs isolation.

**Geography:** Unknown; not established by cited source. **Availability caveat:** Public documentation found; current orderability, support term and installed configuration unverified.

## 11. SolidRun — IIOT-200-8M

**industrial; manufacturer/channel. Score 5 (Linux 2, boundary 2, friction 1).

**Documented:** Gateway provides Linux/Yocto BSP and serial/CAN-FD/Ethernet interfaces. [Primary source](https://www.solid-run.com/iiot200-8m-gateway/). Source basis: Vendor documentation.

**OTA evidence:** Unknown for this product; other SolidSense products must not be generalized.

**Inferred pilot:** Read-only field-data collector deployment. **Inferred migration/sales/safety friction:** BSP revisions and end-OEM ownership; gateway qualification.

**Geography:** Unknown; not established by cited source. **Availability caveat:** Public documentation found; current orderability, support term and installed configuration unverified.

## 12. SYS TEC electronic — sysWORXX CTR-500

**industrial; manufacturer/channel. Score 5 (Linux 2, boundary 2, friction 1).

**Documented:** Vendor flyer describes a freely programmable Linux gateway. [Primary source](https://www.systec-electronic.com/media/default/Redakteur/Nur_zum_Download/neueflyer/Flyer-CTR-SYSTEC-electronic-englisch.pdf). Source basis: Vendor documentation.

**OTA evidence:** Unknown.

**Inferred pilot:** Read-only Linux monitoring component. **Inferred migration/sales/safety friction:** Controller safety boundary, older flyer and current SKU verification.

**Geography:** Unknown; not established by cited source. **Availability caveat:** Older/preliminary technical evidence; reconfirm current product, ownership, orderability and support before discovery. Direct host retrieval failed during research; technical facts use indexed vendor material, not verified current sales availability.

## 13. Variscite — VAR-SOM platforms

**industrial; manufacturer/channel. Score 5 (Linux 2, boundary 2, friction 1).

**Documented:** Vendor documents SWUpdate integration with its Linux Yocto build. [Primary source](https://dev.variscite.com/var-som-6ul/yocto/swupdate-guide/). Source basis: Vendor documentation.

**OTA evidence:** SWUpdate integration guide; installed fleet service unknown. [Source](https://dev.variscite.com/var-som-6ul/yocto/swupdate-guide/).

**Inferred pilot:** Reference application bundle with OEM-controlled SWUpdate. **Inferred migration/sales/safety friction:** Guide targets specific module; end OEM owns approvals and boot layout.

**Geography:** Unknown; not established by cited source. **Availability caveat:** Public documentation found; current orderability, support term and installed configuration unverified.

## 14. AAEON — BOXER-8622AI

**industrial; manufacturer/channel. Score 4 (Linux 2, boundary 2, friction 2).

**Documented:** Manual lists Ubuntu/JetPack, NVIDIA container runtime and built-in Allxon DMS. [Primary source](https://data-us.aaeon.com/DOWNLOAD/MANUAL/BOXER-8622AI%20Manual%201st%20Ed.pdf). Source basis: Vendor documentation.

**OTA evidence:** Allxon DMS listed in product manual; deployment-specific use unknown. [Source](https://data-us.aaeon.com/DOWNLOAD/MANUAL/BOXER-8622AI%20Manual%201st%20Ed.pdf).

**Inferred pilot:** Application-only vision update alongside existing management. **Inferred migration/sales/safety friction:** Existing management agent and JetPack version coupling.

**Geography:** Unknown; not established by cited source. **Availability caveat:** Public documentation found; current orderability, support term and installed configuration unverified.

## 15. Digi International — IX40

**industrial; manufacturer/platform vendor. Score 4 (Linux 2, boundary 2, friction 2).

**Documented:** Vendor documents running Ignition Edge in LXC on ARM64 IX40 DAL OS. [Primary source](https://www.digi.com/support/knowledge-base/ignition-edge-on-digi-dal-os-devices). Source basis: Vendor documentation.

**OTA evidence:** DAL OS update lifecycle documented; fleet provider unknown. [Source](https://docs.digi.com/resources/documentation/digidocs/PDFs/90002537.pdf).

**Inferred pilot:** Non-network-critical collector container. **Inferred migration/sales/safety friction:** musl/container limits and vendor router lifecycle.

**Geography:** Unknown; not established by cited source. **Availability caveat:** Public documentation found; current orderability, support term and installed configuration unverified.

## 16. Eurotech — ReliaGATE 10-20

**industrial; manufacturer/platform vendor. Score 4 (Linux 2, boundary 2, friction 2).

**Documented:** Gateway runs Everyware Linux and Everyware Software Framework based on Eclipse Kura. [Primary source](https://www.eurotech.com/en/products/iot/multi-service-iot-edge-gateways/reliagate-10-20). Source basis: Vendor documentation.

**OTA evidence:** Everyware framework/cloud stack documented; installed OTA configuration unknown.

**Inferred pilot:** Separate application integrity/audit comparison. **Inferred migration/sales/safety friction:** Java/OSGi integration and existing cloud ecosystem; enterprise friction.

**Geography:** Unknown; not established by cited source. **Availability caveat:** Public documentation found; current orderability, support term and installed configuration unverified.

## 17. Kuva Systems — Gas imaging cameras

**energy; manufacturer. Score 4 (Linux 2, boundary 2, friction 2).

**Documented:** Camera-to-cloud equipment monitors methane and VOC emissions. [Primary source](https://www.kuvasystems.com/resources/kuva-systems-camera-and-tower-user-manual). Source basis: Vendor documentation.

**OTA evidence:** Mender case study documents Yocto and Azure IoT Edge updates (2023). [Source](https://mender.io/resources/case-studies/kuva-case-study).

**Inferred pilot:** Non-safety image analytics bundle with measured release cost. **Inferred migration/sales/safety friction:** Incumbent Mender; hazardous-site and emissions compliance assessment.

**Geography:** United States and Canada market; supplier case study. **Availability caveat:** Public documentation found; current orderability, support term and installed configuration unverified.

## 18. Moxa — UC-8200

**industrial; manufacturer. Score 4 (Linux 2, boundary 2, friction 2).

**Documented:** UC-8200 offers Moxa Industrial Linux; MIL3 variants have IEC 62443-4-2 SL2 certification. [Primary source](https://www.moxa.com/en/products/industrial-computing/arm-based-computers/uc-8200-series). Source basis: Vendor documentation.

**OTA evidence:** Vendor OS images published; fleet OTA provider unknown.

**Inferred pilot:** Isolated data collector while preserving vendor OS lifecycle. **Inferred migration/sales/safety friction:** Certified configuration changes require assessment; MIL variants differ.

**Geography:** Unknown; not established by cited source. **Availability caveat:** Public documentation found; current orderability, support term and installed configuration unverified.

## 19. Octave Energy — Battery systems and EMS

**energy; manufacturer/integrator. Score 4 (Linux 2, boundary 2, friction 2).

**Documented:** EMS integrates battery, solar and EV energy assets. [Primary source](https://octave.energy/en/energy-management-system/). Source basis: Vendor documentation.

**OTA evidence:** Mender case study names Revolution Pi and EdgeBox targets (2025). [Source](https://mender.io/resources/case-studies/octave-energy-case-study).

**Inferred pilot:** EMS analytics sidecar excluding battery protection/control. **Inferred migration/sales/safety friction:** Established Mender pipeline; battery safety and energy-market integration.

**Geography:** Brussels, Belgium; supplier case study. **Availability caveat:** Public documentation found; current orderability, support term and installed configuration unverified.

## 20. Toradex — Torizon module platforms

**industrial; manufacturer/platform vendor. Score 4 (Linux 2, boundary 2, friction 2).

**Documented:** Torizon Linux integrates containers and remote system/application updates. [Primary source](https://developer.toradex.com/software/cloud-services/over-the-air-update-solutions/). Source basis: Vendor documentation.

**OTA evidence:** Torizon Cloud/OSTree remote updates documented. [Source](https://developer.toradex.com/software/cloud-services/over-the-air-update-solutions/).

**Inferred pilot:** Channel/reference architecture only; avoid full-stack migration premise. **Inferred migration/sales/safety friction:** Direct commercial overlap; OEM rather than module supplier owns end fleet.

**Geography:** Unknown; not established by cited source. **Availability caveat:** Public documentation found; current orderability, support term and installed configuration unverified.

## 21. Winnow — Vision kitchen waste systems

**commercial; manufacturer/integrator. Score 4 (Linux 2, boundary 2, friction 2).

**Documented:** Vision combines on-site camera hardware and software for kitchen food-waste capture. [Primary source](https://www.winnowsolutions.com/resources/news/visiongen-5-the-next-generation-of-food-waste-tracking-built-for-zero-disruption-in-the-kitchen-1?hs_amp=true). Source basis: Vendor documentation.

**OTA evidence:** balena case study documents balenaOS/balenaCloud (2025). [Source](https://blog.balena.io/winnow-using-ai-and-balena-to-help-kitchens-pinpoint-waste-and-cut-costs/).

**Inferred pilot:** Camera model/application release audit on test device. **Inferred migration/sales/safety friction:** Existing container/cloud tooling; kitchen images/privacy; confirm Gen5 stack.

**Geography:** Unknown; not established by cited source. **Availability caveat:** Public documentation found; current orderability, support term and installed configuration unverified.

## 22. Agrointelli — ROBOTTI

**industrial; manufacturer. Score 3 (Linux 2, boundary 1, friction 2).

**Documented:** ROBOTTI is an autonomous agricultural implement carrier. [Primary source](https://agrointelli.com/new-robot/). Source basis: Vendor documentation.

**OTA evidence:** Mender interview documents Jetson TX2/Xavier and full-system updates (2022). [Source](https://mender.io/blog/autonomous-farming-robots-ota-updates).

**Inferred pilot:** Offline perception-model distribution; never motion control first. **Inferred migration/sales/safety friction:** Autonomous motion safety, incumbent rollback stack; older evidence.

**Geography:** Unknown; not established by cited source. **Availability caveat:** Older/preliminary technical evidence; reconfirm current product, ownership, orderability and support before discovery. Direct host retrieval failed during research; technical facts use indexed vendor material, not verified current sales availability.

## 23. Axis Communications — AXIS OS network cameras

**commercial; manufacturer/platform vendor. Score 3 (Linux 2, boundary 1, friction 2).

**Documented:** AXIS OS is Linux-based and has multiple supported release tracks. [Primary source](https://help.axis.com/en-us/axis-os-release-notes). Source basis: Vendor documentation.

**OTA evidence:** AXIS Device Manager provides firmware upgrades. [Source](https://help.axis.com/en-us/axis-device-manager).

**Inferred pilot:** Supported camera application distribution; preserve AXIS OS authority. **Inferred migration/sales/safety friction:** Signed firmware, application sandbox, surveillance privacy and VMS compatibility.

**Geography:** Unknown; not established by cited source. **Availability caveat:** Public documentation found; current orderability, support term and installed configuration unverified.

## 24. chargebyte — Charge Control C

**energy; manufacturer/channel. Score 3 (Linux 2, boundary 1, friction 2).

**Documented:** Charge Control C is an open-system AC controller running Linux. [Primary source](https://chargebyte.com/assets/downloads/charge_control_c_datasheet_rev6.pdf). Source basis: Vendor documentation.

**OTA evidence:** Unknown.

**Inferred pilot:** OEM-approved ancillary Linux app in lab charger. **Inferred migration/sales/safety friction:** Electrical safety; OEM controls charger integration and approval.

**Geography:** Unknown; not established by cited source. **Availability caveat:** Public documentation found; current orderability, support term and installed configuration unverified.

## 25. Phoenix Contact — PLCnext Control

**industrial; manufacturer/platform vendor. Score 3 (Linux 2, boundary 1, friction 2).

**Documented:** PLCnext architecture uses Linux with a real-time patch. [Primary source](https://www.phoenixcontact.com/assets/b0e8523d-a9e7-48d4-9332-68b1f9620bbd/epaper/EN_PLCnext_Technology_LoRes.pdf). Source basis: Vendor documentation.

**OTA evidence:** Unknown.

**Inferred pilot:** Read-only sidecar only; exclude control logic. **Inferred migration/sales/safety friction:** PLC runtime and certified automation boundary; enterprise procurement.

**Geography:** Unknown; not established by cited source. **Availability caveat:** Public documentation found; current orderability, support term and installed configuration unverified.

## 26. Siemens — SIMATIC IOT2050

**industrial; manufacturer/platform vendor. Score 3 (Linux 2, boundary 1, friction 2).

**Documented:** Official meta-iot2050 repository supplies gateway BSP information. [Primary source](https://github.com/siemens/meta-iot2050/blob/master/README.md). Source basis: Vendor documentation.

**OTA evidence:** Industrial Edge integration documented; specific fleet incumbent unknown. [Source](https://press.siemens.com/global/en/pressrelease/new-siemens-gateway-between-cloud-company-it-and-production).

**Inferred pilot:** Standalone non-control application on example image. **Inferred migration/sales/safety friction:** Enterprise procurement and Industrial Edge overlap; control isolation.

**Geography:** Unknown; not established by cited source. **Availability caveat:** Public documentation found; current orderability, support term and installed configuration unverified.

## 27. WAGO — PFC200

**industrial; manufacturer. Score 3 (Linux 2, boundary 1, friction 2).

**Documented:** PFC200 combines a CODESYS runtime and real-time-capable Linux. [Primary source](https://www.wago.com/ca-en/automation-technology/plcs/pfc200). Source basis: Vendor documentation.

**OTA evidence:** Unknown.

**Inferred pilot:** Non-control diagnostics component in laboratory. **Inferred migration/sales/safety friction:** PLC timing and safety isolation; runtime support contracts.

**Geography:** Unknown; not established by cited source. **Availability caveat:** Public documentation found; current orderability, support term and installed configuration unverified.

## 28. Hilscher — netFIELD gateways

**industrial; manufacturer/platform vendor. Score 2 (Linux 1, boundary 2, friction 2).

**Documented:** netFIELD centrally configures gateways and distributes edge software as containers. [Primary source](https://www.hilscher.com/in/products/centralized-edge-management). Source basis: Vendor documentation.

**OTA evidence:** netFIELD centralized container management documented. [Source](https://www.hilscher.com/in/products/centralized-edge-management).

**Inferred pilot:** Application provenance comparison without replacing OS management. **Inferred migration/sales/safety friction:** Direct platform overlap and existing orchestration; enterprise review.

**Geography:** Unknown; not established by cited source. **Availability caveat:** Public documentation found; current orderability, support term and installed configuration unverified.

## 29. MacGregor — Maritime connected equipment

**industrial; manufacturer. Score 1 (Linux 1, boundary 1, friction 2).

**Documented:** Vendor-associated case study describes cargo/load equipment and remote updates. [Primary source](https://mender.io/resources/case-studies/macgregor-case-study). Source basis: Supplier case study; historical relationship claim.

**OTA evidence:** Mender OTA use documented (2022). [Source](https://mender.io/resources/case-studies/macgregor-case-study).

**Inferred pilot:** Non-control diagnostics package; marine approval discovery. **Inferred migration/sales/safety friction:** Marine certification, intermittent links and established incumbent.

**Geography:** Unknown; not established by cited source. **Availability caveat:** Older/preliminary technical evidence; reconfirm current product, ownership, orderability and support before discovery.

## 30. Robustel — R5020

**industrial; manufacturer/platform vendor. Score 1 (Linux 1, boundary 1, friction 2).

**Documented:** Industrial 5G router offers optional RCMS and Edge2Cloud applications. [Primary source](https://robustel.com/product/r5020/). Source basis: Vendor documentation.

**OTA evidence:** RCMS option documented; installed OTA provider unknown.

**Inferred pilot:** Data bridge application outside routing path. **Inferred migration/sales/safety friction:** Router resources and supported app packaging; existing remote management.

**Geography:** Unknown; not established by cited source. **Availability caveat:** Public documentation found; current orderability, support term and installed configuration unverified.

## 31. Schindler — Ahead connected elevators

**commercial; manufacturer/platform vendor. Score 1 (Linux 1, boundary 1, friction 2).

**Documented:** Ahead offers connected elevator services and passenger media. [Primary source](https://www.schindler.com/en/innovations/schindler-ahead.html). Source basis: Vendor documentation.

**OTA evidence:** Mender interview describes North American IoT deployment (2021). [Source](https://mender.io/blog/smart-elevator-ota-software-updates).

**Inferred pilot:** Passenger information/analytics only; exclude elevator control. **Inferred migration/sales/safety friction:** Passenger safety, regional deployment scope and long enterprise sales.

**Geography:** Unknown; not established by cited source. **Availability caveat:** Older/preliminary technical evidence; reconfirm current product, ownership, orderability and support before discovery.

## 32. Teltonika Networks — RutOS routers/gateways

**industrial; manufacturer/platform vendor. Score 1 (Linux 1, boundary 1, friction 2).

**Documented:** Vendor documents firmware updates through WebUI, RMS and CLI. [Primary source](https://wiki.teltonika-networks.com/view/RutOS_and_TSWOS_firmware_update). Source basis: Vendor documentation.

**OTA evidence:** Teltonika RMS firmware management documented. [Source](https://wiki.teltonika-networks.com/view/RutOS_and_TSWOS_firmware_update).

**Inferred pilot:** Application payload audit only; preserve router firmware path. **Inferred migration/sales/safety friction:** Direct RMS overlap; network outage impact; supported package constraints.

**Geography:** Unknown; not established by cited source. **Availability caveat:** Public documentation found; current orderability, support term and installed configuration unverified.

## 33. Vaillant Group — Connected heating gateways

**energy; manufacturer. Score 1 (Linux 1, boundary 1, friction 2).

**Documented:** Vendor compliance statement identifies sensoNET VR921 as a gateway. [Primary source](https://professional.vaillant.co.uk/downloads/aproducts/statement-of-compliance/sensonet-vr921-psti-compliance-29-04-2024-2858450.pdf). Source basis: Vendor documentation.

**OTA evidence:** Mender documents selection for next-generation heating products; exact SKU deployment unknown. [Source](https://mender.io/resources/topic/building-management).

**Inferred pilot:** Non-control gateway diagnostics pending SKU/runtime qualification. **Inferred migration/sales/safety friction:** Do not map Mender claim to VR921 without confirmation; heating safety and enterprise sales.

**Geography:** Unknown; not established by cited source. **Availability caveat:** Public documentation found; current orderability, support term and installed configuration unverified.

## 34. Victron Energy — Cerbo GX

**energy; manufacturer. Score 1 (Linux 1, boundary 1, friction 2).

**Documented:** Venus OS Large supports Cerbo GX with Node-RED and Signal K. [Primary source](https://www.victronenergy.com/live/venus-os%3Alarge?do=export_pdf). Source basis: Vendor documentation.

**OTA evidence:** Venus OS internet and removable-media updates documented; fleet orchestration unknown. [Source](https://www.victronenergy.com/media/pg/Cerbo_GX/en/firmware-updates.html).

**Inferred pilot:** Read-only energy analytics flow distribution. **Inferred migration/sales/safety friction:** Energy-control risk and constrained CPU; keep OEM firmware path.

**Geography:** Unknown; not established by cited source. **Availability caveat:** Public documentation found; current orderability, support term and installed configuration unverified.

## 35. Weidmuller — u-control

**industrial; manufacturer/platform vendor. Score 1 (Linux 1, boundary 1, friction 2).

**Documented:** Vendor describes u-control web with container technology for machinery and plant applications. [Primary source](https://www.weidmueller.com/int/company/press/trade_press_news/u_control_web_with_container_technology_for_more_flexibility_in_industrial_applications.jsp). Source basis: Vendor documentation.

**OTA evidence:** easyConnect supports u-control connections; installed OTA provider unknown. [Source](https://www.weidmueller.com/int/service/industrial_service_platform/apps_and_services/index.jsp).

**Inferred pilot:** Containerized read-only machine-data adapter. **Inferred migration/sales/safety friction:** Control boundary and existing industrial service platform.

**Geography:** Unknown; not established by cited source. **Availability caveat:** Public documentation found; current orderability, support term and installed configuration unverified.

## 36. BrightSign — Digital signage players

**commercial; manufacturer/platform vendor. Score -1 (Linux 0, boundary 1, friction 2).

**Documented:** BrightSign publishes production OS releases for its signage players. [Primary source](https://support.brightsign.biz/hc/en-us/articles/4403013330459-New-Production-Version-of-OS8-2-Available). Source basis: Vendor documentation.

**OTA evidence:** Vendor OS release channel documented; fleet OTA platform unknown.

**Inferred pilot:** Discovery-only until supported runtime established. **Inferred migration/sales/safety friction:** Linux agent access unverified; proprietary player lifecycle and signage uptime.

**Geography:** Unknown; not established by cited source. **Availability caveat:** Public documentation found; current orderability, support term and installed configuration unverified.

## 37. Formlabs — Form/Fuse printers

**commercial; manufacturer/platform vendor. Score -1 (Linux 0, boundary 1, friction 2).

**Documented:** Dashboard supports group firmware management with model-specific versions. [Primary source](https://formlabs.com/support/Using-the-Bulk-Firmware-Updater-in-Dashboard/). Source basis: Vendor documentation.

**OTA evidence:** Formlabs Dashboard bulk firmware updater documented. [Source](https://formlabs.com/support/Using-the-Bulk-Firmware-Updater-in-Dashboard/).

**Inferred pilot:** Ancillary job monitoring; do not replace printer firmware. **Inferred migration/sales/safety friction:** Existing group update tooling; resin/laser/process safety and closed runtime.

**Geography:** Unknown; not established by cited source. **Availability caveat:** Public documentation found; current orderability, support term and installed configuration unverified.

## 38. Prusa Research — Prusa Connect printers

**commercial; manufacturer/platform vendor. Score -1 (Linux 0, boundary 1, friction 2).

**Documented:** Prusa Connect supports remote control/monitoring and managing multiple printers. [Primary source](https://www.prusa3d.com/p/prusa-connect/). Source basis: Vendor documentation.

**OTA evidence:** Prusa Connect documented; OTA support varies by model and is unverified here.

**Inferred pilot:** External fleet gateway adjunct; do not propose MCU agent. **Inferred migration/sales/safety friction:** No Linux host compatibility established; thermal/motion safety and print interruption.

**Geography:** Unknown; not established by cited source. **Availability caveat:** Public documentation found; current orderability, support term and installed configuration unverified.

## 39. UltiMaker — S3/S5/S7 printers

**commercial; manufacturer/platform vendor. Score -1 (Linux 0, boundary 1, friction 2).

**Documented:** Vendor publishes network/security firmware releases for S-series printers. [Primary source](https://support.ultimaker.com/articles/Knowledge/1667410781982). Source basis: Vendor documentation.

**OTA evidence:** Vendor firmware channel documented; installed fleet backend unknown.

**Inferred pilot:** Qualification of application runtime before any pilot. **Inferred migration/sales/safety friction:** Model-specific image transitions and thermal/motion safety.

**Geography:** Unknown; not established by cited source. **Availability caveat:** Public documentation found; current orderability, support term and installed configuration unverified.

## 40. Alfen — Eve charging stations

**energy; manufacturer. Score -2 (Linux 0, boundary 0, friction 2).

**Documented:** Vendor documents remote backoffice updates plus onsite installer/app methods. [Primary source](https://aceservice.alfen.com/en-us/knowledgebase/article/KA-01108). Source basis: Vendor documentation.

**OTA evidence:** Backoffice-managed firmware documented; specific operator platform unknown. [Source](https://aceservice.alfen.com/en-us/knowledgebase/article/KA-01108).

**Inferred pilot:** Discovery of supported application boundary before pilot. **Inferred migration/sales/safety friction:** Charge safety, backoffice authority and intermediate firmware/key migrations.

**Geography:** Unknown; not established by cited source. **Availability caveat:** Public documentation found; current orderability, support term and installed configuration unverified.

## 41. Enphase Energy — IQ Gateway

**energy; manufacturer/platform vendor. Score -2 (Linux 0, boundary 0, friction 2).

**Documented:** Enphase publishes gateway releases and remote upgrade applicability by region and connectivity. [Primary source](https://enphase.com/en-au/installers/resources/documentation/communication?f%5B0%5D=product_media_name%3A2644&f%5B1%5D=product_media_name%3A2741&f%5B2%5D=product_media_name%3A3080&f%5B3%5D=product_media_name%3A5002&f%5B4%5D=product_media_name%3A10280&field_alternative_language=All&page=3&search_api_language=All). Source basis: Vendor documentation.

**OTA evidence:** Enphase remote software upgrades documented. [Source](https://enphase.com/en-au/installers/resources/documentation/communication?f%5B0%5D=product_media_name%3A2644&f%5B1%5D=product_media_name%3A2741&f%5B2%5D=product_media_name%3A3080&f%5B3%5D=product_media_name%3A5002&f%5B4%5D=product_media_name%3A10280&field_alternative_language=All&page=3&search_api_language=All).

**Inferred pilot:** Discovery-only: supported peripheral integration boundary. **Inferred migration/sales/safety friction:** Grid/storage safety, region-specific releases and closed ecosystem.

**Geography:** Unknown; not established by cited source. **Availability caveat:** Public documentation found; current orderability, support term and installed configuration unverified.

## 42. Fronius — Datamanager

**energy; manufacturer. Score -2 (Linux 0, boundary 0, friction 2).

**Documented:** Manual documents firmware update over LAN or web and automatic update search. [Primary source](https://www.fronius.com/~/downloads/Solar%20Energy/Operating%20Instructions/42%2C0426%2C0191%2CEA.pdf). Source basis: Vendor documentation.

**OTA evidence:** Fronius web/LAN update workflow documented. [Source](https://www.fronius.com/~/downloads/Solar%20Energy/Operating%20Instructions/42%2C0426%2C0191%2CEA.pdf).

**Inferred pilot:** Adjacent monitoring gateway; no inverter firmware pilot. **Inferred migration/sales/safety friction:** Inverter/grid safety; supported third-party runtime unknown.

**Geography:** Unknown; not established by cited source. **Availability caveat:** Public documentation found; current orderability, support term and installed configuration unverified.

## 43. HMS Networks — Ewon Flexy

**industrial; manufacturer/platform vendor. Score -2 (Linux 0, boundary 0, friction 2).

**Documented:** HMS documents Flexy firmware maintenance and eBuddy update tooling. [Primary source](https://www.hms-networks.com/support/tech-support/kb-articles/33479819503378-Temporary-FTP-Activation-on-Ewon-Flexy-and-Cosy-for-Backup-and-Firmware-Update). Source basis: Vendor documentation.

**OTA evidence:** Vendor/eBuddy firmware path documented; installed OTA provider unknown.

**Inferred pilot:** Discovery-only until supported runtime/agent confirmed. **Inferred migration/sales/safety friction:** No supported Linux agent established; industrial network access and enterprise review.

**Geography:** Unknown; not established by cited source. **Availability caveat:** Public documentation found; current orderability, support term and installed configuration unverified.

## 44. Kempower — Charging stations

**energy; manufacturer/platform vendor. Score -2 (Linux 0, boundary 0, friction 2).

**Documented:** Commissioning Tool checks and updates charging station firmware. [Primary source](https://tip.kempower.com/r/doc/mykempower-hardware-portal/initial-actions). Source basis: Vendor documentation.

**OTA evidence:** Kempower Commissioning Tool documented; fleet OTA backend unknown. [Source](https://tip.kempower.com/r/doc/mykempower-hardware-portal/initial-actions).

**Inferred pilot:** Ancillary diagnostics only after runtime qualification. **Inferred migration/sales/safety friction:** Electrical safety, charge-session coordination and OEM service workflow.

**Geography:** Unknown; not established by cited source. **Availability caveat:** Public documentation found; current orderability, support term and installed configuration unverified.

## 45. RATIONAL — iCombi Pro / iVario

**commercial; manufacturer/platform vendor. Score -2 (Linux 0, boundary 0, friction 2).

**Documented:** ConnectedCooking distributes cooking programs and updates appliance software. [Primary source](https://www.rational-online.com/en_ca/products/index.php). Source basis: Vendor documentation.

**OTA evidence:** ConnectedCooking update workflow documented. [Source](https://www.rational-online.com/en_ca/products/index.php).

**Inferred pilot:** Discovery-only for separate non-control gateway. **Inferred migration/sales/safety friction:** Food safety, heat/process control, proprietary appliance software.

**Geography:** Unknown; not established by cited source. **Availability caveat:** Public documentation found; current orderability, support term and installed configuration unverified.

## 46. SMA Solar Technology — Data Manager M

**energy; manufacturer/platform vendor. Score -2 (Linux 0, boundary 0, friction 2).

**Documented:** Data Manager centrally downloads updates and distributes them through a PV system. [Primary source](https://www.sma.de/en/products/energy-management/data-manager-m-10). Source basis: Vendor documentation.

**OTA evidence:** SMA automatic firmware update path documented. [Source](https://manuals.sma.de/EDMM-10A/en-US/4145434379.html).

**Inferred pilot:** Discovery of non-control extension capability first. **Inferred migration/sales/safety friction:** Grid/control approval; model generations; existing ennexOS ecosystem.

**Geography:** Unknown; not established by cited source. **Availability caveat:** Public documentation found; current orderability, support term and installed configuration unverified.

## 47. SolarEdge — Commercial storage

**energy; manufacturer/platform vendor. Score -2 (Linux 0, boundary 0, friction 2).

**Documented:** Commercial storage release notes describe remote firmware upgrades coordinated with support. [Primary source](https://solaredge.com/en/release-notes/commercial-storage). Source basis: Vendor documentation.

**OTA evidence:** SolarEdge support-coordinated remote updates documented. [Source](https://solaredge.com/en/release-notes/commercial-storage).

**Inferred pilot:** Read-only external monitoring; no storage-control replacement. **Inferred migration/sales/safety friction:** Battery/grid safety and enterprise support approvals.

**Geography:** Unknown; not established by cited source. **Availability caveat:** Public documentation found; current orderability, support term and installed configuration unverified.

## 48. sonnen — sonnenBatterie

**energy; manufacturer/platform vendor. Score -2 (Linux 0, boundary 0, friction 2).

**Documented:** Vendor release notes describe automatic internet-delivered software updates. [Primary source](https://www.sonnen.de/rln-sb). Source basis: Vendor documentation.

**OTA evidence:** sonnen automatic updates documented. [Source](https://www.sonnen.de/rln-sb).

**Inferred pilot:** External monitoring only; no battery firmware replacement. **Inferred migration/sales/safety friction:** Battery protection, VPP authority and mandatory release handling.

**Geography:** Unknown; not established by cited source. **Availability caveat:** Public documentation found; current orderability, support term and installed configuration unverified.

## 49. Wallbox — Commander 2 / Pulsar Plus

**energy; manufacturer/platform vendor. Score -2 (Linux 0, boundary 0, friction 2).

**Documented:** Vendor documents remote cloud updates and local Bluetooth updates with model-specific prerequisites. [Primary source](https://support.wallbox.com/en/knowledge-base/how-to-update-your-chargers-software-with-the-mywallbox-app/). Source basis: Vendor documentation.

**OTA evidence:** Wallbox app/cloud update workflow documented. [Source](https://support.wallbox.com/en/knowledge-base/how-to-update-your-chargers-software-with-the-mywallbox-app/).

**Inferred pilot:** Discovery-only; external analytics gateway if OEM approves. **Inferred migration/sales/safety friction:** Charging interruption risk, proprietary images and app ownership.

**Geography:** Unknown; not established by cited source. **Availability caveat:** Public documentation found; current orderability, support term and installed configuration unverified.

## 50. Zaptec — Zaptec Pro

**energy; manufacturer/platform vendor. Score -2 (Linux 0, boundary 0, friction 2).

**Documented:** Pro release notes describe full-OS updates and native OCPP on supported hardware. [Primary source](https://help.zaptec.com/documentation/zaptec-pro-firmware-updates?lang=en-GB). Source basis: Vendor documentation.

**OTA evidence:** Zaptec Portal/app firmware path documented. [Source](https://docs.zaptec.com/docs/zaptec-go-ocpp16j-configuration-guide).

**Inferred pilot:** Non-control observability; runtime access unestablished. **Inferred migration/sales/safety friction:** Charge safety, mixed hardware versions and existing cloud authorization.

**Geography:** Unknown; not established by cited source. **Availability caveat:** Public documentation found; current orderability, support term and installed configuration unverified.
