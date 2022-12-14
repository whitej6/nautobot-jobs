import requests
from django.utils.text import slugify
from django.contrib.contenttypes.models import ContentType
from nautobot.extras.jobs import Job, ChoiceVar
from nautobot.ipam.models import Prefix, IPAddress
from nautobot.dcim.models import Region, Site, DeviceRole, Manufacturer, Platform, Device, DeviceType, Cable
from nautobot.extras.models import Status

AIRPORTS_URL = (
    "https://pkgstore.datahub.io/core/airport-codes/airport-codes_json/"
    "data/9ca22195b4c64a562a0a8be8d133e700/airport-codes_json.json"
)

CHOICES = (
    (100, "5k Devices"),
    (200, "10k Devices"),
    (500, "25k Devices"),
    (1000, "50k Devices"),
    (2000, "100k Devices"),
    (5000, "250k Devices"),
    (9000, "450k Devices")
)

CONTINENT_MAP = {
    'AN': "Antartica",
    'SA': "South America",
    'AF': "Africa",
    'AS': "Asia",
    'NA': "North America",
    'EU': "Europe",
    'OC': "Oceania"
}

IFACE_CT = ContentType.objects.get(app_label="dcim", model="interface")
CABLE_STATUS = Status.objects.get(name="Connected")

class PopulateNautobot(Job):
    num_sites = ChoiceVar(choices=CHOICES, required=False)
    roles = ["wan", "dist", "bb", "access"]

    def _get_airport_sites(self, num_sites):
        resp = requests.get(AIRPORTS_URL)
        unique_airports = {i['iata_code'] :i for i in resp.json() if i['iata_code']}
        if not num_sites:
            list(unique_airports.values())
        return list(unique_airports.values())[:int(num_sites)]

    def _create_parent_regions(self):
        mapper = {}
        for k, v in CONTINENT_MAP.items():
            mapper.update({k: Region.objects.get_or_create(name=v, slug=slugify(v))[0]})
        return mapper

    def _get_dev_type(self, role):
        if role == "wan":
            return DeviceType.objects.get(model="MX104-PREMIUM")
        if role == "dist":
            return DeviceType.objects.get(model="DCS-7048-T")
        if role == "bb":
            return DeviceType.objects.get(model="Nexus 9396TX")
        return DeviceType.objects.get(model="Catalyst 9300L-48P-4X")

    def _create_sites(self, sites):
        active = Status.objects.get(name="Active")
        self.parent_prefix = Prefix.objects.get_or_create(network="10.0.0.0", prefix_length=8, status=active)[0]
        self.log_info("Creating Parent Regions.")
        continents = self._create_parent_regions()
        retired = Status.objects.get(name="Retired")
        self.log_info("Creating Site, Country Regions, and Prefixes.")
        for site in sites:
            country = Region.objects.get_or_create(name=site["iso_country"], slug=slugify(site["iso_country"]), parent=continents[site['continent']])[0]
            site = Site.objects.get_or_create(
                name=site['iata_code']+"-01",
                slug=slugify(site['iata_code']+"-01"),
                region=country,
                status=retired if site['iata_code'] == 'closed' else active,
                facility=site['name'],
                description=f"{site['name']} located in {site['municipality']}"
            )[0]
            prefix = str(self.parent_prefix.get_first_available_prefix().network)
            Prefix.objects.create(network=prefix, prefix_length=22, site=site, status=active)

    def _create_device_roles(self):
        self.log_info("Creating Device Roles.")
        dev_roles = {}
        for i in self.roles:
            dev_roles.update(
                {
                    i: {
                        "role": DeviceRole.objects.get_or_create(name=i, slug=i)[0],
                        "type": self._get_dev_type(i)
                    }
                }
            )
        return dev_roles


    def _create_platforms(self):
        self.log_info("Creating Platforms.")
        cisco = Manufacturer.objects.get(name="Cisco")
        juniper = Manufacturer.objects.get(name="Juniper")
        arista = Manufacturer.objects.get(name="Arista")
        return {
            "access": Platform.objects.get_or_create(name="cisco_ios", slug="cisco_ios", manufacturer=cisco)[0],
            "bb": Platform.objects.get_or_create(name="cisco_nxos", slug="cisco_nxos", manufacturer=cisco)[0],
            "wan": Platform.objects.get_or_create(name="juniper_junos", slug="juniper_junos", manufacturer=juniper)[0],
            "dist": Platform.objects.get_or_create(name="arista_eos", slug="arista_eos", manufacturer=arista)[0],
        }

    def _connect_devices(self, dev1, dev2, prefix):
        active = Status.objects.get(name="Active")
        iface1 = dev1.interfaces.filter(cable__isnull=True).exclude(mgmt_only=True).first()
        iface2 = dev2.interfaces.filter(cable__isnull=True).exclude(mgmt_only=True).first()
        prefix = str(prefix.get_first_available_prefix().network)
        prefix = Prefix.objects.create(network=prefix, prefix_length=31, site=dev1.site, is_pool=True, status=active)
        ip = prefix.get_first_available_ip().split("/")
        IPAddress(host=ip[0], prefix_length=ip[1], assigned_object_type=IFACE_CT, assigned_object_id=iface1.id, status=active).validated_save()
        ip = prefix.get_first_available_ip().split("/")
        IPAddress(host=ip[0], prefix_length=ip[1], assigned_object_type=IFACE_CT, assigned_object_id=iface2.id, status=active).validated_save()
        Cable.objects.create(
            termination_a_type=IFACE_CT,
            termination_b_type=IFACE_CT,
            termination_a_id=iface1.id,
            termination_b_id=iface2.id,
            type="cat6",
            _termination_a_device=dev1,
            _termination_b_device=dev2,
            status=CABLE_STATUS
        )

    def _create_devices(self):
        roles = self._create_device_roles()
        platforms = self._create_platforms()
        status = Status.objects.get(name="Active")
        for site in Site.objects.all():
            prefix = Prefix.objects.filter(site=site).first()
            wan1 = Device.objects.get_or_create(
                name=f"{site.slug}-wan-01",
                platform=platforms["wan"],
                device_role=roles["wan"]["role"],
                device_type=roles["wan"]["type"],
                site=site,
                status=status
            )[0]
            wan2 = Device.objects.get_or_create(
                name=f"{site.slug}-wan-02",
                platform=platforms["wan"],
                device_role=roles["wan"]["role"],
                device_type=roles["wan"]["type"],
                site=site,
                status=status
            )[0]
            self._connect_devices(wan1, wan2, prefix)
            bb1 = Device.objects.get_or_create(
                name=f"{site.slug}-bb-01",
                platform=platforms["bb"],
                device_role=roles["bb"]["role"],
                device_type=roles["bb"]["type"],
                site=site,
                status=status
            )[0]
            bb2 = Device.objects.get_or_create(
                name=f"{site.slug}-bb-02",
                platform=platforms["bb"],
                device_role=roles["bb"]["role"],
                device_type=roles["bb"]["type"],
                site=site,
                status=status
            )[0]
            self._connect_devices(wan1, bb1, prefix)
            self._connect_devices(wan2, bb2, prefix)
            dist1 = Device.objects.get_or_create(
                name=f"{site.slug}-dist-01",
                platform=platforms["dist"],
                device_role=roles["dist"]["role"],
                device_type=roles["dist"]["type"],
                site=site,
                status=status
            )[0]
            dist2 = Device.objects.get_or_create(
                name=f"{site.slug}-dist-02",
                platform=platforms["dist"],
                device_role=roles["dist"]["role"],
                device_type=roles["dist"]["type"],
                site=site,
                status=status
            )[0]
            self._connect_devices(bb1, dist1, prefix)
            self._connect_devices(bb2, dist2, prefix)
            for i in range(44):
                access = Device.objects.get_or_create(
                    name=f"{site.slug}-access-0{i}",
                    platform=platforms["access"],
                    device_role=roles["access"]["role"],
                    device_type=roles["access"]["type"],
                    site=site,
                    status=status
                )[0]
                self._connect_devices(access, dist1, prefix)
                self._connect_devices(access, dist2, prefix)
            self.log_info(f"{site.name} fully created.")

    def run(self, data, commit):
        self.log_info("Gathering Site Codes.")
        num_sites = data.get("num_sites")
        unique_airports = self._get_airport_sites(num_sites)
        self.log_info(f"Creating {num_sites}.")
        self._create_sites(unique_airports)
        self.log_info("Sites created.")

    def post_run(self):
        self.log_info("Creating Devices.")
        self._create_devices()
