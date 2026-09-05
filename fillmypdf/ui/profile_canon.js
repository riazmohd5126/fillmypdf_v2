/** Flat profile keys → canonical paths (Guided Fill + Guided Batch CSV preview). */
(function () {
  const PROFILE_TO_CANON = {
    patient_first_name: "patient.first_name",
    patient_last_name: "patient.last_name",
    patient_dob: "patient.dob",
    patient_gender: "patient.sex",
    patient_sex: "patient.sex",
    patient_address: "patient.address_line1",
    patient_city: "patient.city",
    patient_state: "patient.state",
    patient_zip: "patient.zip",
    patient_phone: "patient.phone",
    patient_email: "patient.email",
    patient_full_name: "patient.full_name",
    member_id: "insurance.member_id",
    group_number: "insurance.group_number",
    insurance_plan: "insurance.payer_name",
    payer_name: "insurance.payer_name",
    subscriber_name: "insurance.subscriber_name",
    provider_first_name: "prescriber.first_name",
    provider_last_name: "prescriber.last_name",
    provider_full_name: "prescriber.full_name",
    npi: "prescriber.npi",
    specialty: "prescriber.specialty",
    practice_name: "facility.name",
    provider_address: "prescriber.address_line1",
    provider_city: "prescriber.city",
    provider_state: "prescriber.state",
    provider_zip: "prescriber.zip",
    provider_phone: "prescriber.phone",
    provider_fax: "prescriber.fax",
    dea_number: "prescriber.dea",
    provider_tax_id: "prescriber.tax_id",
    first_name: "patient.first_name",
    last_name: "patient.last_name",
    dob: "patient.dob",
    email: "patient.email",
    phone: "patient.phone",
    address: "patient.address_line1",
    city: "patient.city",
    state: "patient.state",
    zip: "patient.zip",
    company_name: "facility.name",
    employer_name: "facility.name",
    employer_address: "facility.address_line1",
    ein: "facility.tax_id",
    facility_name: "facility.name",
    facility_npi: "facility.npi",
    facility_phone: "facility.phone",
    facility_fax: "facility.fax",
    facility_address: "facility.address_line1",
    facility_city: "facility.city",
    facility_state: "facility.state",
    facility_zip: "facility.zip",
    facility_tax_id: "facility.tax_id",
    pharmacy_name: "pharmacy.name",
    pharmacy_npi: "pharmacy.npi",
    pharmacy_phone: "pharmacy.phone",
    pharmacy_fax: "pharmacy.fax",
    pharmacy_address: "pharmacy.address_line1",
    pharmacy_city: "pharmacy.city",
    pharmacy_state: "pharmacy.state",
    pharmacy_zip: "pharmacy.zip",
    pharmacy_store_number: "pharmacy.store_number",
    drug_name: "medication.drug_name",
    strength: "medication.strength",
    sig: "medication.sig",
    quantity: "medication.quantity",
    days_supply: "medication.days_supply",
    dosage_form: "medication.dosage_form",
    hcpcs_jcode: "medication.hcpcs_jcode",
    ndc: "medication.ndc",
    frequency: "medication.frequency",
    diagnosis_code: "clinical.primary_diagnosis_code",
    diagnosis_description: "clinical.primary_diagnosis_description",
    clinical_rationale: "clinical.clinical_rationale",
    previous_auth_number: "request.previous_auth_number",
    requested_duration: "request.requested_duration",
    request_type: "request.request_type",
    date_of_request: "request.date_of_request",
    patient_patient_first_name: "patient.first_name",
    provider_npi: "prescriber.npi",
    provider_provider_phone: "prescriber.phone",
    provider_specialty: "prescriber.specialty",
  };

  const ROLE_KEY_OVERRIDES = {
    facility: {
      npi: "facility.npi",
      phone: "facility.phone",
      fax: "facility.fax",
      address: "facility.address_line1",
      city: "facility.city",
      state: "facility.state",
      zip: "facility.zip",
      practice_name: "facility.name",
      company_name: "facility.name",
      provider_phone: "facility.phone",
      provider_fax: "facility.fax",
      provider_address: "facility.address_line1",
      provider_city: "facility.city",
      provider_state: "facility.state",
      provider_zip: "facility.zip",
      provider_tax_id: "facility.tax_id",
    },
    pharmacy: {
      npi: "pharmacy.npi",
      phone: "pharmacy.phone",
      fax: "pharmacy.fax",
      address: "pharmacy.address_line1",
      city: "pharmacy.city",
      state: "pharmacy.state",
      zip: "pharmacy.zip",
      practice_name: "pharmacy.name",
      company_name: "pharmacy.name",
      pharmacy_name: "pharmacy.name",
    },
    encounter: {},
  };

  const ROLE_MIRROR_PREFIXES = [
    ["prescriber", "requesting_provider"],
    ["prescriber", "attending_provider"],
    ["prescriber", "billing_provider"],
  ];

  function normalizeSex(v) {
    const s = String(v || "").trim().toLowerCase();
    if (!s) return v;
    if (s === "m" || s === "male" || s === "man") return "M";
    if (s === "f" || s === "female" || s === "woman") return "F";
    if (s === "u" || s === "unknown" || s === "other" || s === "x" || s === "non-binary")
      return "U";
    return v;
  }

  function composeNames(mapped) {
    const pf = mapped["patient.first_name"],
      pl = mapped["patient.last_name"];
    if ((pf || pl) && !mapped["patient.full_name"])
      mapped["patient.full_name"] = [pf, pl].filter(Boolean).join(" ");
    const prf = mapped["prescriber.first_name"],
      prl = mapped["prescriber.last_name"];
    if ((prf || prl) && !mapped["prescriber.full_name"])
      mapped["prescriber.full_name"] = [prf, prl].filter(Boolean).join(" ");
  }

  function expandRoleAliases(mapped, role) {
    if (role === "provider" || !role) {
      const keys = Object.keys(mapped);
      ROLE_MIRROR_PREFIXES.forEach(([from, to]) => {
        keys.forEach((k) => {
          if (!k.startsWith(from + ".")) return;
          const alt = to + k.slice(from.length);
          if (mapped[alt] === undefined || mapped[alt] === "") mapped[alt] = mapped[k];
        });
      });
      if (mapped["requesting_provider.full_name"] && !mapped["prescriber.contact_name"])
        mapped["prescriber.contact_name"] = mapped["requesting_provider.full_name"];
      if (mapped["prescriber.full_name"] && !mapped["prescriber.contact_name"])
        mapped["prescriber.contact_name"] = mapped["prescriber.full_name"];
    }
    if (mapped["patient.full_name"] && !mapped["insurance.subscriber_name"])
      mapped["insurance.subscriber_name"] = mapped["patient.full_name"];
    if (mapped["facility.name"] && !mapped["prescriber.practice_name"])
      mapped["prescriber.practice_name"] = mapped["facility.name"];
  }

  function flatToCanonMap(flat, role) {
    const mapped = {};
    const overrides = ROLE_KEY_OVERRIDES[role] || {};
    Object.keys(flat || {}).forEach((k) => {
      const raw = flat[k];
      if (raw === undefined || raw === null || raw === "") return;
      if (k.includes(".") || k.startsWith("q:") || k.startsWith("t:")) {
        mapped[k] = raw;
        return;
      }
      const canon = overrides[k] || PROFILE_TO_CANON[k];
      if (!canon) return;
      let v = raw;
      if (canon === "patient.sex") v = normalizeSex(v);
      mapped[canon] = v;
    });
    composeNames(mapped);
    expandRoleAliases(mapped, role);
    return mapped;
  }

  window.PROFILE_TO_CANON = PROFILE_TO_CANON;
  window.ROLE_KEY_OVERRIDES = ROLE_KEY_OVERRIDES;
  window.ROLE_MIRROR_PREFIXES = ROLE_MIRROR_PREFIXES;
  window.normalizeSex = normalizeSex;
  window.composeNames = composeNames;
  window.expandRoleAliases = expandRoleAliases;
  window.flatToCanonMap = flatToCanonMap;
  window.fmpFlatToCanonMap = flatToCanonMap;
})();
