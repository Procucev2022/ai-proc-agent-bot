"""
pincode_lookup.py
-----------------
Utility module for fetching city and state information
for Indian postal pincodes using offline postal directory mapping
with online India Post API enrichment and fallback.
"""

import requests
import logging
import time
import re
from typing import Dict, Optional

logger = logging.getLogger(__name__)

# Local in-memory cache to avoid duplicate network calls
_PINCODE_CACHE: Dict[str, Dict[str, str]] = {}

# Comprehensive Indian Postal 3-digit prefix mapping to Sorting District and State
# Derived from India Post PIN Allocation Standards
POSTAL_PREFIX_DIRECTORY: Dict[str, Dict[str, str]] = {
    # ── 1x: Northern Zone (Delhi, Haryana, Punjab, HP, J&K, Ladakh, Chandigarh) ──
    "110": {"city": "New Delhi", "state": "Delhi"},
    "111": {"city": "Delhi", "state": "Delhi"},
    "121": {"city": "Faridabad", "state": "Haryana"},
    "122": {"city": "Gurugram", "state": "Haryana"},
    "123": {"city": "Rewari", "state": "Haryana"},
    "124": {"city": "Rohtak", "state": "Haryana"},
    "125": {"city": "Hisar", "state": "Haryana"},
    "126": {"city": "Jind", "state": "Haryana"},
    "127": {"city": "Bhiwani", "state": "Haryana"},
    "128": {"city": "Kaithal", "state": "Haryana"},
    "131": {"city": "Sonipat", "state": "Haryana"},
    "132": {"city": "Panipat", "state": "Haryana"},
    "133": {"city": "Ambala", "state": "Haryana"},
    "134": {"city": "Panchkula", "state": "Haryana"},
    "135": {"city": "Yamunanagar", "state": "Haryana"},
    "136": {"city": "Kurukshetra", "state": "Haryana"},
    "140": {"city": "Mohali / Rupnagar", "state": "Punjab"},
    "141": {"city": "Ludhiana", "state": "Punjab"},
    "142": {"city": "Moga", "state": "Punjab"},
    "143": {"city": "Amritsar", "state": "Punjab"},
    "144": {"city": "Jalandhar", "state": "Punjab"},
    "145": {"city": "Pathankot / Gurdaspur", "state": "Punjab"},
    "146": {"city": "Hoshiarpur", "state": "Punjab"},
    "147": {"city": "Patiala", "state": "Punjab"},
    "148": {"city": "Sangrur", "state": "Punjab"},
    "151": {"city": "Bathinda", "state": "Punjab"},
    "152": {"city": "Firozpur", "state": "Punjab"},
    "160": {"city": "Chandigarh", "state": "Chandigarh"},
    "171": {"city": "Shimla", "state": "Himachal Pradesh"},
    "172": {"city": "Kinnaur", "state": "Himachal Pradesh"},
    "173": {"city": "Solan", "state": "Himachal Pradesh"},
    "174": {"city": "Bilaspur", "state": "Himachal Pradesh"},
    "175": {"city": "Mandi", "state": "Himachal Pradesh"},
    "176": {"city": "Kangra", "state": "Himachal Pradesh"},
    "177": {"city": "Hamirpur", "state": "Himachal Pradesh"},
    "180": {"city": "Jammu", "state": "Jammu and Kashmir"},
    "181": {"city": "Jammu / Samba", "state": "Jammu and Kashmir"},
    "182": {"city": "Udhampur", "state": "Jammu and Kashmir"},
    "184": {"city": "Kathua", "state": "Jammu and Kashmir"},
    "185": {"city": "Rajouri / Poonch", "state": "Jammu and Kashmir"},
    "190": {"city": "Srinagar", "state": "Jammu and Kashmir"},
    "191": {"city": "Ganderbal", "state": "Jammu and Kashmir"},
    "192": {"city": "Anantnag / Pulwama", "state": "Jammu and Kashmir"},
    "193": {"city": "Baramulla", "state": "Jammu and Kashmir"},
    "194": {"city": "Leh / Ladakh", "state": "Ladakh"},

    # ── 2x: Northern Zone (Uttar Pradesh, Uttarakhand) ──
    "201": {"city": "Ghaziabad / Noida", "state": "Uttar Pradesh"},
    "202": {"city": "Aligarh", "state": "Uttar Pradesh"},
    "203": {"city": "Bulandshahr", "state": "Uttar Pradesh"},
    "204": {"city": "Hathras", "state": "Uttar Pradesh"},
    "205": {"city": "Mainpuri", "state": "Uttar Pradesh"},
    "206": {"city": "Etawah", "state": "Uttar Pradesh"},
    "207": {"city": "Etah", "state": "Uttar Pradesh"},
    "208": {"city": "Kanpur", "state": "Uttar Pradesh"},
    "209": {"city": "Kanpur Dehat", "state": "Uttar Pradesh"},
    "210": {"city": "Banda / Chitrakoot", "state": "Uttar Pradesh"},
    "211": {"city": "Prayagraj (Allahabad)", "state": "Uttar Pradesh"},
    "212": {"city": "Fatehpur / Kaushambi", "state": "Uttar Pradesh"},
    "221": {"city": "Varanasi", "state": "Uttar Pradesh"},
    "222": {"city": "Jaunpur", "state": "Uttar Pradesh"},
    "223": {"city": "Azamgarh", "state": "Uttar Pradesh"},
    "224": {"city": "Ayodhya (Faizabad)", "state": "Uttar Pradesh"},
    "225": {"city": "Barabanki", "state": "Uttar Pradesh"},
    "226": {"city": "Lucknow", "state": "Uttar Pradesh"},
    "227": {"city": "Lucknow Rural / Raebareli", "state": "Uttar Pradesh"},
    "228": {"city": "Sultanpur", "state": "Uttar Pradesh"},
    "229": {"city": "Raebareli", "state": "Uttar Pradesh"},
    "230": {"city": "Pratapgarh", "state": "Uttar Pradesh"},
    "231": {"city": "Mirzapur", "state": "Uttar Pradesh"},
    "232": {"city": "Chandauli", "state": "Uttar Pradesh"},
    "233": {"city": "Ghazipur", "state": "Uttar Pradesh"},
    "241": {"city": "Hardoi", "state": "Uttar Pradesh"},
    "242": {"city": "Shahjahanpur", "state": "Uttar Pradesh"},
    "243": {"city": "Bareilly", "state": "Uttar Pradesh"},
    "244": {"city": "Moradabad", "state": "Uttar Pradesh"},
    "245": {"city": "Hapur", "state": "Uttar Pradesh"},
    "246": {"city": "Bijnor", "state": "Uttar Pradesh"},
    "247": {"city": "Saharanpur", "state": "Uttar Pradesh"},
    "248": {"city": "Dehradun", "state": "Uttarakhand"},
    "249": {"city": "Haridwar / Rishikesh", "state": "Uttarakhand"},
    "250": {"city": "Meerut", "state": "Uttar Pradesh"},
    "251": {"city": "Muzaffarnagar", "state": "Uttar Pradesh"},
    "261": {"city": "Sitapur", "state": "Uttar Pradesh"},
    "262": {"city": "Pilibhit / Lakhimpur", "state": "Uttar Pradesh"},
    "263": {"city": "Nainital / Haldwani", "state": "Uttarakhand"},
    "271": {"city": "Gonda / Balrampur", "state": "Uttar Pradesh"},
    "272": {"city": "Basti", "state": "Uttar Pradesh"},
    "273": {"city": "Gorakhpur", "state": "Uttar Pradesh"},
    "274": {"city": "Deoria", "state": "Uttar Pradesh"},
    "281": {"city": "Mathura", "state": "Uttar Pradesh"},
    "282": {"city": "Agra", "state": "Uttar Pradesh"},
    "283": {"city": "Firozabad", "state": "Uttar Pradesh"},
    "284": {"city": "Jhansi", "state": "Uttar Pradesh"},

    # ── 3x: Western Zone (Rajasthan, Gujarat, Daman & Diu, Dadra & Nagar Haveli) ──
    "301": {"city": "Alwar", "state": "Rajasthan"},
    "302": {"city": "Jaipur", "state": "Rajasthan"},
    "303": {"city": "Jaipur Rural", "state": "Rajasthan"},
    "304": {"city": "Tonk", "state": "Rajasthan"},
    "305": {"city": "Ajmer", "state": "Rajasthan"},
    "306": {"city": "Pali", "state": "Rajasthan"},
    "307": {"city": "Sirohi", "state": "Rajasthan"},
    "311": {"city": "Bhilwara", "state": "Rajasthan"},
    "312": {"city": "Chittorgarh", "state": "Rajasthan"},
    "313": {"city": "Udaipur", "state": "Rajasthan"},
    "314": {"city": "Dungarpur", "state": "Rajasthan"},
    "321": {"city": "Bharatpur", "state": "Rajasthan"},
    "322": {"city": "Sawai Madhopur", "state": "Rajasthan"},
    "324": {"city": "Kota", "state": "Rajasthan"},
    "325": {"city": "Baran", "state": "Rajasthan"},
    "326": {"city": "Jhalawar", "state": "Rajasthan"},
    "331": {"city": "Churu", "state": "Rajasthan"},
    "332": {"city": "Sikar", "state": "Rajasthan"},
    "333": {"city": "Jhunjhunu", "state": "Rajasthan"},
    "334": {"city": "Bikaner", "state": "Rajasthan"},
    "335": {"city": "Sri Ganganagar", "state": "Rajasthan"},
    "341": {"city": "Nagaur", "state": "Rajasthan"},
    "342": {"city": "Jodhpur", "state": "Rajasthan"},
    "343": {"city": "Jalore", "state": "Rajasthan"},
    "344": {"city": "Barmer", "state": "Rajasthan"},
    "345": {"city": "Jaisalmer", "state": "Rajasthan"},
    "360": {"city": "Rajkot", "state": "Gujarat"},
    "361": {"city": "Jamnagar", "state": "Gujarat"},
    "362": {"city": "Junagadh", "state": "Gujarat"},
    "363": {"city": "Surendranagar", "state": "Gujarat"},
    "364": {"city": "Bhavnagar", "state": "Gujarat"},
    "365": {"city": "Amreli", "state": "Gujarat"},
    "370": {"city": "Kutch (Bhuj / Gandhidham)", "state": "Gujarat"},
    "380": {"city": "Ahmedabad", "state": "Gujarat"},
    "382": {"city": "Gandhinagar / Ahmedabad Rural", "state": "Gujarat"},
    "383": {"city": "Sabarkantha / Himatnagar", "state": "Gujarat"},
    "384": {"city": "Mehsana / Patan", "state": "Gujarat"},
    "385": {"city": "Banaskantha (Palanpur)", "state": "Gujarat"},
    "387": {"city": "Kheda / Nadiad", "state": "Gujarat"},
    "388": {"city": "Anand", "state": "Gujarat"},
    "389": {"city": "Panchmahal / Godhra", "state": "Gujarat"},
    "390": {"city": "Vadodara", "state": "Gujarat"},
    "391": {"city": "Vadodara Rural", "state": "Gujarat"},
    "392": {"city": "Bharuch", "state": "Gujarat"},
    "393": {"city": "Ankleshwar / Narmada", "state": "Gujarat"},
    "394": {"city": "Surat Rural", "state": "Gujarat"},
    "395": {"city": "Surat", "state": "Gujarat"},
    "396": {"city": "Valsad / Vapi / Daman & Diu", "state": "Gujarat"},

    # ── 4x: Western Zone (Maharashtra, Goa, Madhya Pradesh, Chhattisgarh) ──
    "400": {"city": "Mumbai", "state": "Maharashtra"},
    "401": {"city": "Thane / Palghar", "state": "Maharashtra"},
    "402": {"city": "Raigad", "state": "Maharashtra"},
    "403": {"city": "North Goa / South Goa", "state": "Goa"},
    "410": {"city": "Lonavala / Pune Rural", "state": "Maharashtra"},
    "411": {"city": "Pune", "state": "Maharashtra"},
    "412": {"city": "Pune District", "state": "Maharashtra"},
    "413": {"city": "Solapur / Ahmednagar", "state": "Maharashtra"},
    "414": {"city": "Ahmednagar", "state": "Maharashtra"},
    "415": {"city": "Satara / Ratnagiri", "state": "Maharashtra"},
    "416": {"city": "Kolhapur / Sangli", "state": "Maharashtra"},
    "421": {"city": "Kalyan / Dombivli", "state": "Maharashtra"},
    "422": {"city": "Nashik", "state": "Maharashtra"},
    "423": {"city": "Malegaon / Nashik Rural", "state": "Maharashtra"},
    "424": {"city": "Dhule", "state": "Maharashtra"},
    "425": {"city": "Jalgaon", "state": "Maharashtra"},
    "431": {"city": "Chhatrapati Sambhajinagar (Aurangabad)", "state": "Maharashtra"},
    "440": {"city": "Nagpur", "state": "Maharashtra"},
    "441": {"city": "Nagpur Rural / Bhandara", "state": "Maharashtra"},
    "442": {"city": "Wardha / Chandrapur", "state": "Maharashtra"},
    "444": {"city": "Amravati / Akola", "state": "Maharashtra"},
    "445": {"city": "Yavatmal", "state": "Maharashtra"},
    "450": {"city": "Khandwa", "state": "Madhya Pradesh"},
    "451": {"city": "Khargone", "state": "Madhya Pradesh"},
    "452": {"city": "Indore", "state": "Madhya Pradesh"},
    "453": {"city": "Indore Rural / Dhar", "state": "Madhya Pradesh"},
    "454": {"city": "Dhar", "state": "Madhya Pradesh"},
    "455": {"city": "Dewas", "state": "Madhya Pradesh"},
    "456": {"city": "Ujjain", "state": "Madhya Pradesh"},
    "457": {"city": "Ratlam", "state": "Madhya Pradesh"},
    "458": {"city": "Mandsaur", "state": "Madhya Pradesh"},
    "460": {"city": "Betul", "state": "Madhya Pradesh"},
    "461": {"city": "Hoshangabad (Narmadapuram)", "state": "Madhya Pradesh"},
    "462": {"city": "Bhopal", "state": "Madhya Pradesh"},
    "464": {"city": "Vidisha", "state": "Madhya Pradesh"},
    "465": {"city": "Shajapur / Rajgarh", "state": "Madhya Pradesh"},
    "470": {"city": "Sagar", "state": "Madhya Pradesh"},
    "471": {"city": "Chhatarpur", "state": "Madhya Pradesh"},
    "472": {"city": "Tikamgarh", "state": "Madhya Pradesh"},
    "474": {"city": "Gwalior", "state": "Madhya Pradesh"},
    "475": {"city": "Datia / Gwalior Rural", "state": "Madhya Pradesh"},
    "476": {"city": "Morena", "state": "Madhya Pradesh"},
    "477": {"city": "Bhind", "state": "Madhya Pradesh"},
    "480": {"city": "Chhindwara", "state": "Madhya Pradesh"},
    "481": {"city": "Balaghat / Seoni", "state": "Madhya Pradesh"},
    "482": {"city": "Jabalpur", "state": "Madhya Pradesh"},
    "483": {"city": "Katni", "state": "Madhya Pradesh"},
    "484": {"city": "Shahdol / Umaria", "state": "Madhya Pradesh"},
    "485": {"city": "Satna", "state": "Madhya Pradesh"},
    "486": {"city": "Rewa", "state": "Madhya Pradesh"},
    "490": {"city": "Durg / Bhilai", "state": "Chhattisgarh"},
    "491": {"city": "Rajnandgaon", "state": "Chhattisgarh"},
    "492": {"city": "Raipur", "state": "Chhattisgarh"},
    "493": {"city": "Raipur Rural / Mahasamund", "state": "Chhattisgarh"},
    "494": {"city": "Bastar / Jagdalpur", "state": "Chhattisgarh"},
    "495": {"city": "Bilaspur", "state": "Chhattisgarh"},
    "496": {"city": "Raigarh", "state": "Chhattisgarh"},
    "497": {"city": "Surguja / Ambikapur", "state": "Chhattisgarh"},

    # ── 5x: Southern Zone (Andhra Pradesh, Telangana, Karnataka) ──
    "500": {"city": "Hyderabad", "state": "Telangana"},
    "501": {"city": "Rangareddy", "state": "Telangana"},
    "502": {"city": "Medak / Sangareddy", "state": "Telangana"},
    "503": {"city": "Nizamabad", "state": "Telangana"},
    "504": {"city": "Adilabad", "state": "Telangana"},
    "505": {"city": "Karimnagar", "state": "Telangana"},
    "506": {"city": "Warangal", "state": "Telangana"},
    "507": {"city": "Khammam", "state": "Telangana"},
    "508": {"city": "Nalgonda", "state": "Telangana"},
    "509": {"city": "Mahabubnagar", "state": "Telangana"},
    "515": {"city": "Anantapur", "state": "Andhra Pradesh"},
    "516": {"city": "Kadapa (YSR)", "state": "Andhra Pradesh"},
    "517": {"city": "Chittoor / Tirupati", "state": "Andhra Pradesh"},
    "518": {"city": "Kurnool", "state": "Andhra Pradesh"},
    "520": {"city": "Vijayawada (NTR)", "state": "Andhra Pradesh"},
    "521": {"city": "Krishna / Machilipatnam", "state": "Andhra Pradesh"},
    "522": {"city": "Guntur", "state": "Andhra Pradesh"},
    "523": {"city": "Prakasam / Ongole", "state": "Andhra Pradesh"},
    "524": {"city": "Nellore", "state": "Andhra Pradesh"},
    "530": {"city": "Visakhapatnam", "state": "Andhra Pradesh"},
    "531": {"city": "Anakapalli / Vizag Rural", "state": "Andhra Pradesh"},
    "532": {"city": "Srikakulam", "state": "Andhra Pradesh"},
    "533": {"city": "Kakinada / East Godavari", "state": "Andhra Pradesh"},
    "534": {"city": "West Godavari / Eluru", "state": "Andhra Pradesh"},
    "535": {"city": "Vizianagaram", "state": "Andhra Pradesh"},
    "560": {"city": "Bengaluru (Bangalore)", "state": "Karnataka"},
    "561": {"city": "Bengaluru Rural / Chikkaballapur", "state": "Karnataka"},
    "562": {"city": "Ramanagara / Kolar", "state": "Karnataka"},
    "563": {"city": "Kolar", "state": "Karnataka"},
    "570": {"city": "Mysuru (Mysore)", "state": "Karnataka"},
    "571": {"city": "Mandya / Chamrajnagar", "state": "Karnataka"},
    "572": {"city": "Tumakuru (Tumkur)", "state": "Karnataka"},
    "573": {"city": "Hassan", "state": "Karnataka"},
    "574": {"city": "Dakshina Kannada / Udupi", "state": "Karnataka"},
    "575": {"city": "Mangaluru (Mangalore)", "state": "Karnataka"},
    "576": {"city": "Udupi", "state": "Karnataka"},
    "577": {"city": "Shivamogga / Davanagere / Chikkamagaluru", "state": "Karnataka"},
    "580": {"city": "Hubballi-Dharwad", "state": "Karnataka"},
    "581": {"city": "Uttara Kannada / Haveri", "state": "Karnataka"},
    "582": {"city": "Gadag", "state": "Karnataka"},
    "583": {"city": "Ballari (Bellary) / Vijayanagara", "state": "Karnataka"},
    "584": {"city": "Raichur / Koppal", "state": "Karnataka"},
    "585": {"city": "Kalaburagi (Gulbarga) / Bidar", "state": "Karnataka"},
    "586": {"city": "Vijayapura (Bijapur)", "state": "Karnataka"},
    "587": {"city": "Bagalkote", "state": "Karnataka"},
    "590": {"city": "Belagavi (Belgaum)", "state": "Karnataka"},
    "591": {"city": "Belagavi Rural", "state": "Karnataka"},

    # ── 6x: Southern Zone (Tamil Nadu, Kerala, Puducherry, Lakshadweep) ──
    "600": {"city": "Chennai", "state": "Tamil Nadu"},
    "601": {"city": "Tiruvallur", "state": "Tamil Nadu"},
    "602": {"city": "Kanchipuram", "state": "Tamil Nadu"},
    "603": {"city": "Chengalpattu", "state": "Tamil Nadu"},
    "604": {"city": "Villupuram", "state": "Tamil Nadu"},
    "605": {"city": "Puducherry / Cuddalore", "state": "Puducherry"},
    "606": {"city": "Tiruvannamalai / Kallakurichi", "state": "Tamil Nadu"},
    "607": {"city": "Cuddalore", "state": "Tamil Nadu"},
    "608": {"city": "Chidambaram", "state": "Tamil Nadu"},
    "609": {"city": "Mayiladuthurai / Nagapattinam", "state": "Tamil Nadu"},
    "611": {"city": "Nagapattinam", "state": "Tamil Nadu"},
    "612": {"city": "Kumbakonam", "state": "Tamil Nadu"},
    "613": {"city": "Thanjavur", "state": "Tamil Nadu"},
    "614": {"city": "Tiruvarur", "state": "Tamil Nadu"},
    "620": {"city": "Tiruchirappalli (Trichy)", "state": "Tamil Nadu"},
    "621": {"city": "Perambalur / Ariyalur", "state": "Tamil Nadu"},
    "622": {"city": "Pudukkottai", "state": "Tamil Nadu"},
    "623": {"city": "Ramanathapuram", "state": "Tamil Nadu"},
    "624": {"city": "Dindigul", "state": "Tamil Nadu"},
    "625": {"city": "Madurai", "state": "Tamil Nadu"},
    "626": {"city": "Virudhunagar", "state": "Tamil Nadu"},
    "627": {"city": "Tirunelveli / Tenkasi", "state": "Tamil Nadu"},
    "628": {"city": "Thoothukudi (Tuticorin)", "state": "Tamil Nadu"},
    "629": {"city": "Kanniyakumari (Nagercoil)", "state": "Tamil Nadu"},
    "630": {"city": "Sivaganga / Karaikudi", "state": "Tamil Nadu"},
    "631": {"city": "Ranipet / Arakkonam", "state": "Tamil Nadu"},
    "632": {"city": "Vellore", "state": "Tamil Nadu"},
    "635": {"city": "Krishnagiri / Hosur", "state": "Tamil Nadu"},
    "636": {"city": "Salem", "state": "Tamil Nadu"},
    "637": {"city": "Namakkal", "state": "Tamil Nadu"},
    "638": {"city": "Erode", "state": "Tamil Nadu"},
    "639": {"city": "Karur", "state": "Tamil Nadu"},
    "641": {"city": "Coimbatore", "state": "Tamil Nadu"},
    "642": {"city": "Pollachi / Tiruppur", "state": "Tamil Nadu"},
    "643": {"city": "The Nilgiris (Ooty)", "state": "Tamil Nadu"},
    "670": {"city": "Kannur", "state": "Kerala"},
    "671": {"city": "Kasaragod", "state": "Kerala"},
    "673": {"city": "Kozhikode (Calicut) / Wayanad", "state": "Kerala"},
    "676": {"city": "Malappuram", "state": "Kerala"},
    "678": {"city": "Palakkad", "state": "Kerala"},
    "679": {"city": "Shoranur / Ottapalam", "state": "Kerala"},
    "680": {"city": "Thrissur", "state": "Kerala"},
    "682": {"city": "Ernakulam / Kochi", "state": "Kerala"},
    "683": {"city": "Aluva / Ernakulam Rural", "state": "Kerala"},
    "685": {"city": "Idukki", "state": "Kerala"},
    "686": {"city": "Kottayam", "state": "Kerala"},
    "688": {"city": "Alappuzha (Alleppey)", "state": "Kerala"},
    "689": {"city": "Pathanamthitta", "state": "Kerala"},
    "690": {"city": "Kollam Rural", "state": "Kerala"},
    "691": {"city": "Kollam (Quilon)", "state": "Kerala"},
    "695": {"city": "Thiruvananthapuram (Trivandrum)", "state": "Kerala"},

    # ── 7x: Eastern Zone (West Bengal, Odisha, North Eastern States) ──
    "700": {"city": "Kolkata", "state": "West Bengal"},
    "711": {"city": "Howrah", "state": "West Bengal"},
    "712": {"city": "Hooghly", "state": "West Bengal"},
    "713": {"city": "Purba & Paschim Bardhaman (Durgapur/Asansol)", "state": "West Bengal"},
    "721": {"city": "Paschim Medinipur / Kharagpur", "state": "West Bengal"},
    "722": {"city": "Bankura", "state": "West Bengal"},
    "723": {"city": "Purulia", "state": "West Bengal"},
    "731": {"city": "Birbhum", "state": "West Bengal"},
    "732": {"city": "Malda", "state": "West Bengal"},
    "733": {"city": "Uttar Dinajpur", "state": "West Bengal"},
    "734": {"city": "Darjeeling / Siliguri", "state": "West Bengal"},
    "735": {"city": "Jalpaiguri / Alipurduar", "state": "West Bengal"},
    "736": {"city": "Cooch Behar", "state": "West Bengal"},
    "737": {"city": "Gangtok", "state": "Sikkim"},
    "741": {"city": "Nadia", "state": "West Bengal"},
    "742": {"city": "Murshidabad", "state": "West Bengal"},
    "743": {"city": "North & South 24 Parganas", "state": "West Bengal"},
    "744": {"city": "Port Blair", "state": "Andaman and Nicobar Islands"},
    "751": {"city": "Bhubaneswar (Khordha)", "state": "Odisha"},
    "752": {"city": "Puri", "state": "Odisha"},
    "753": {"city": "Cuttack", "state": "Odisha"},
    "754": {"city": "Jagatsinghpur / Kendrapara", "state": "Odisha"},
    "755": {"city": "Jajpur", "state": "Odisha"},
    "756": {"city": "Balasore / Bhadrak", "state": "Odisha"},
    "757": {"city": "Mayurbhanj / Keonjhar", "state": "Odisha"},
    "758": {"city": "Keonjhar", "state": "Odisha"},
    "759": {"city": "Dhenkanal / Angul", "state": "Odisha"},
    "760": {"city": "Berhampur (Ganjam)", "state": "Odisha"},
    "761": {"city": "Ganjam / Gajapati", "state": "Odisha"},
    "762": {"city": "Kandhamal / Boudh", "state": "Odisha"},
    "764": {"city": "Koraput / Rayagada", "state": "Odisha"},
    "766": {"city": "Kalahandi / Nuapada", "state": "Odisha"},
    "767": {"city": "Balangir / Sonepur", "state": "Odisha"},
    "768": {"city": "Sambalpur / Bargarh", "state": "Odisha"},
    "769": {"city": "Rourkela (Sundargarh)", "state": "Odisha"},
    "781": {"city": "Guwahati (Kamrup)", "state": "Assam"},
    "782": {"city": "Nagaon / Morigaon", "state": "Assam"},
    "783": {"city": "Goalpara / Dhubri", "state": "Assam"},
    "784": {"city": "Tezpur (Sonitpur)", "state": "Assam"},
    "785": {"city": "Jorhat / Golaghat", "state": "Assam"},
    "786": {"city": "Dibrugarh / Tinsukia", "state": "Assam"},
    "787": {"city": "Lakhimpur / Dhemaji", "state": "Assam"},
    "788": {"city": "Silchar (Cachar)", "state": "Assam"},
    "791": {"city": "Itanagar", "state": "Arunachal Pradesh"},
    "793": {"city": "Shillong (East Khasi Hills)", "state": "Meghalaya"},
    "794": {"city": "Tura (West Garo Hills)", "state": "Meghalaya"},
    "795": {"city": "Imphal", "state": "Manipur"},
    "796": {"city": "Aizawl", "state": "Mizoram"},
    "797": {"city": "Kohima / Dimapur", "state": "Nagaland"},
    "798": {"city": "Mokokchung", "state": "Nagaland"},
    "799": {"city": "Agartala", "state": "Tripura"},

    # ── 8x: Eastern Zone (Bihar, Jharkhand) ──
    "800": {"city": "Patna", "state": "Bihar"},
    "801": {"city": "Patna Rural / Danapur", "state": "Bihar"},
    "802": {"city": "Bhojpur (Arrah) / Buxar", "state": "Bihar"},
    "803": {"city": "Nalanda (Bihar Sharif)", "state": "Bihar"},
    "804": {"city": "Jehanabad / Arwal", "state": "Bihar"},
    "805": {"city": "Nawada", "state": "Bihar"},
    "811": {"city": "Munger / Lakhisarai", "state": "Bihar"},
    "812": {"city": "Bhagalpur", "state": "Bihar"},
    "813": {"city": "Banka", "state": "Bihar"},
    "814": {"city": "Deoghar / Dumka", "state": "Jharkhand"},
    "815": {"city": "Giridih", "state": "Jharkhand"},
    "816": {"city": "Sahibganj / Pakur", "state": "Jharkhand"},
    "821": {"city": "Rohtas (Sasaram) / Kaimur", "state": "Bihar"},
    "822": {"city": "Palamu / Garhwa", "state": "Jharkhand"},
    "823": {"city": "Gaya", "state": "Bihar"},
    "824": {"city": "Aurangabad", "state": "Bihar"},
    "825": {"city": "Hazaribagh / Ramgarh", "state": "Jharkhand"},
    "826": {"city": "Dhanbad", "state": "Jharkhand"},
    "827": {"city": "Bokaro", "state": "Jharkhand"},
    "828": {"city": "Dhanbad Rural", "state": "Jharkhand"},
    "829": {"city": "Ramgarh", "state": "Jharkhand"},
    "831": {"city": "Jamshedpur (East Singhbhum)", "state": "Jharkhand"},
    "832": {"city": "Ghatshila / Seraikela", "state": "Jharkhand"},
    "833": {"city": "Chaibasa (West Singhbhum)", "state": "Jharkhand"},
    "834": {"city": "Ranchi", "state": "Jharkhand"},
    "835": {"city": "Ranchi Rural / Khunti / Gumla", "state": "Jharkhand"},
    "841": {"city": "Saran (Chapra) / Siwan / Gopalganj", "state": "Bihar"},
    "842": {"city": "Muzaffarpur", "state": "Bihar"},
    "843": {"city": "Sitamarhi / Sheohar", "state": "Bihar"},
    "844": {"city": "Vaishali (Hajipur)", "state": "Bihar"},
    "845": {"city": "East & West Champaran (Motihari/Bettiah)", "state": "Bihar"},
    "846": {"city": "Darbhanga", "state": "Bihar"},
    "847": {"city": "Madhubani", "state": "Bihar"},
    "848": {"city": "Samastipur", "state": "Bihar"},
    "851": {"city": "Begusarai", "state": "Bihar"},
    "852": {"city": "Saharsa / Supaul / Madhepura", "state": "Bihar"},
    "853": {"city": "Khagaria", "state": "Bihar"},
    "854": {"city": "Purnia / Katihar / Araria / Kishanganj", "state": "Bihar"},
    "855": {"city": "Kishanganj", "state": "Bihar"},
}

# 2-digit fallback map for broad states
STATE_PREFIX_MAP: Dict[str, str] = {
    "11": "Delhi",
    "12": "Haryana",
    "13": "Haryana",
    "14": "Punjab",
    "15": "Punjab",
    "16": "Chandigarh",
    "17": "Himachal Pradesh",
    "18": "Jammu and Kashmir",
    "19": "Jammu and Kashmir",
    "20": "Uttar Pradesh",
    "21": "Uttar Pradesh",
    "22": "Uttar Pradesh",
    "23": "Uttar Pradesh",
    "24": "Uttar Pradesh",
    "25": "Uttar Pradesh",
    "26": "Uttar Pradesh",
    "27": "Uttar Pradesh",
    "28": "Uttar Pradesh",
    "30": "Rajasthan",
    "31": "Rajasthan",
    "32": "Rajasthan",
    "33": "Rajasthan",
    "34": "Rajasthan",
    "36": "Gujarat",
    "37": "Gujarat",
    "38": "Gujarat",
    "39": "Gujarat",
    "40": "Maharashtra",
    "41": "Maharashtra",
    "42": "Maharashtra",
    "43": "Maharashtra",
    "44": "Maharashtra",
    "45": "Madhya Pradesh",
    "46": "Madhya Pradesh",
    "47": "Madhya Pradesh",
    "48": "Madhya Pradesh",
    "49": "Chhattisgarh",
    "50": "Telangana",
    "51": "Andhra Pradesh",
    "52": "Andhra Pradesh",
    "53": "Andhra Pradesh",
    "56": "Karnataka",
    "57": "Karnataka",
    "58": "Karnataka",
    "59": "Karnataka",
    "60": "Tamil Nadu",
    "61": "Tamil Nadu",
    "62": "Tamil Nadu",
    "63": "Tamil Nadu",
    "64": "Tamil Nadu",
    "67": "Kerala",
    "68": "Kerala",
    "69": "Kerala",
    "70": "West Bengal",
    "71": "West Bengal",
    "72": "West Bengal",
    "73": "West Bengal",
    "74": "West Bengal",
    "75": "Odisha",
    "76": "Odisha",
    "78": "Assam",
    "79": "North Eastern",
    "80": "Bihar",
    "81": "Bihar",
    "82": "Bihar",
    "83": "Jharkhand",
    "84": "Bihar",
    "85": "Bihar",
}


def lookup_offline_pincode(pincode: str) -> Optional[Dict[str, str]]:
    """
    Instantly resolves City/District and State from the offline Indian postal directory.
    Zero network latency, 100% uptime.
    """
    clean_pincode = str(pincode).strip()
    if not clean_pincode.isdigit() or len(clean_pincode) != 6:
        return None

    # Try 3-digit sorting district prefix match
    prefix_3 = clean_pincode[:3]
    if prefix_3 in POSTAL_PREFIX_DIRECTORY:
        entry = POSTAL_PREFIX_DIRECTORY[prefix_3]
        return {
            "pincode": clean_pincode,
            "city": entry["city"],
            "state": entry["state"]
        }

    # Try 2-digit circle state fallback
    prefix_2 = clean_pincode[:2]
    if prefix_2 in STATE_PREFIX_MAP:
        state = STATE_PREFIX_MAP[prefix_2]
        return {
            "pincode": clean_pincode,
            "city": state,
            "state": state
        }

    return None


def get_pincode_details(pincode, max_retries=1, timeout=3):
    """
    Fetch location details for a given Indian pincode with fast timeout.
    """
    url = f"https://api.postalpincode.in/pincode/{pincode}"
    
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
        'Accept': 'application/json',
        'Connection': 'close'
    }

    for attempt in range(max_retries):
        try:
            response = requests.get(url, headers=headers, timeout=timeout, verify=False)
            response.raise_for_status()
            return response.json()
        except requests.exceptions.RequestException as e:
            logger.debug(f"[PINCODE_LOOKUP] External API call error for {pincode}: {e}")
            if attempt < max_retries - 1:
                time.sleep(1)
    return None


def get_location_from_pincode(pincode: str) -> Optional[Dict[str, str]]:
    """Synchronous version of get_location_from_pincode_async."""
    clean_pincode = str(pincode).strip()
    if not clean_pincode.isdigit() or len(clean_pincode) != 6:
        return None

    # Check cache first
    if clean_pincode in _PINCODE_CACHE:
        return _PINCODE_CACHE[clean_pincode]

    # Check offline postal directory first for instant response
    offline_result = lookup_offline_pincode(clean_pincode)

    # Try external API to enrich with specific Post Office name if available
    try:
        data = get_pincode_details(clean_pincode, max_retries=1, timeout=2.5)
        if data and isinstance(data, list) and len(data) > 0:
            result = data[0]
            if result.get("Status") == "Success" and result.get("PostOffice"):
                post_office = result["PostOffice"][0]
                location = {
                    "pincode": clean_pincode,
                    "city": post_office.get("District") or post_office.get("Name", ""),
                    "state": post_office.get("State", "")
                }
                _PINCODE_CACHE[clean_pincode] = location
                return location
    except Exception as e:
        logger.debug(f"[PINCODE_LOOKUP] Live API enrichment skipped: {e}")

    # Return offline result if live API was slow/offline
    if offline_result:
        _PINCODE_CACHE[clean_pincode] = offline_result
        return offline_result

    return None


async def get_location_from_pincode_async(pincode: str) -> Optional[Dict[str, str]]:
    """
    Get location from pincode using combined Online + Offline Directory lookup.
    Guarantees reliable resolution without external API timeout failures.
    """
    return get_location_from_pincode(pincode)


if __name__ == "__main__":
    for code in ["680581", "385310", "380015", "560001", "110001", "550132", "999999"]:
        print(f"Pincode {code} -> {get_location_from_pincode(code)}")
