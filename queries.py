ACTIVE_QUERY = """
WITH months AS (
    SELECT generate_series(
        DATE '2025-07-01',
        date_trunc('month', CURRENT_DATE)::date,
        INTERVAL '1 month'
    )::date AS month_start
),
month_ref AS (
    SELECT
        month_start,
        LEAST(
            (month_start + INTERVAL '1 month - 1 day')::date,
            CURRENT_DATE - 1
        ) AS ref_date
    FROM months
),
diagnosis_data AS (
    SELECT DISTINCT ON (patient_id)
        patient_id,
        diagnosis_name,
        primary_diagnosis,
        date_updated
    FROM public.patient_provision_diagnosis_treatment
    ORDER BY patient_id, date_updated DESC NULLS LAST
),
plan_history AS (
    SELECT
        patient_rpp_id,
        COUNT(*) OVER (PARTITION BY patient_id ORDER BY enrollment_date::date) AS months_with_us
    FROM public.patient_rpp_registration
),
patient_records AS (
    SELECT DISTINCT ON (prpp.patient_ref_id, prpp.patient_rpp_id)
        pr.patient_name,
        prpp.hosp_name,
        prpp.lead_source,
        prpp.patient_ref_id,
        prpp.amount,
        prpp.assigned_to_name,
        prpp.mobile_number,
        prpp.renewalstatus,
        prpp.enrollment_date::date AS enrollment_date,
        prpp.due_date::date AS due_date,
        prpp.hold_by_name,
        prpp.hold_date,
        pra.status,
        pr.age,
        pr.gender_name,
        pr.family_type_name,
        pr.socio_economic_status_name,
        pr.marital_status_name,
        pr.occupation,
        pr.edu_name,
        prpp.psychiatrist_name,
        prpp.psychologist_name,
        pr.state_id,
        pr.district_id,
        prpp.package_name,
        prpp.package_price,
        'Regular' AS patient_type,
        COALESCE(
            dd.primary_diagnosis,
            (SELECT string_agg(trim(both E' \n\t\r' from elem), ', ')
             FROM jsonb_array_elements_text(dd.diagnosis_name) AS elem)
        ) AS primary_diagnosis,
        ph.months_with_us
    FROM public.patient_rpp_registration prpp
    INNER JOIN public.patient_registration pr
        ON prpp.patient_ref_id = pr.patient_ref_id
    LEFT JOIN public.patient_rpp_assignment pra
        ON prpp.patient_rpp_id = pra.patient_rpp_id
    LEFT JOIN public.patient_csr_terms csr
        ON prpp._id = csr.rppobjectid
    LEFT JOIN diagnosis_data dd
        ON dd.patient_id = pr.patient_id
    LEFT JOIN plan_history ph
        ON ph.patient_rpp_id = prpp.patient_rpp_id
    WHERE prpp.lead_source NOT IN 
          ('CSR', 'Existing Client', 'Offline-Webinar', 'NVF')
      AND csr.rppobjectid IS NULL
)
SELECT
    m.month_start AS active_date,
    p.*
FROM month_ref m
INNER JOIN patient_records p
    ON p.enrollment_date <= m.ref_date
   AND p.due_date >= m.ref_date
ORDER BY m.month_start, p.patient_name;
"""


INACTIVE_QUERY = """
SELECT * FROM (
    SELECT DISTINCT ON (prpp.patient_ref_id)
        pr.patient_name,
        prpp.hosp_name,
        prpp.lead_source,
        prpp.patient_ref_id,
        prpp.amount,
        prpp.assigned_to_name,
        prpp.mobile_number,
        prpp.renewalstatus,
        prpp.enrollment_date::date AS enrollment_date,
        prpp.due_date::date AS due_date,
        (prpp.due_date::date + 1) AS inactive_date,
        prpp.hold_by_name,
        prpp.hold_date,
        pra.status,
        pr.age,
        pr.gender_name,
        pr.family_type_name,
        pr.socio_economic_status_name,
        pr.marital_status_name,
        pr.occupation,
        pr.edu_name,
        prpp.psychiatrist_name,
        prpp.psychologist_name,
        pr.state_id,
        pr.district_id,
        prpp.package_name,
        prpp.package_price,
        'Regular' AS patient_type
    FROM public.patient_rpp_registration prpp
    INNER JOIN public.patient_registration pr
        ON prpp.patient_ref_id = pr.patient_ref_id
    LEFT JOIN public.patient_rpp_assignment pra
        ON prpp.patient_rpp_id = pra.patient_rpp_id
    LEFT JOIN public.patient_csr_terms csr
        ON prpp._id = csr.rppobjectid
    WHERE prpp.lead_source NOT IN 
          ('CSR', 'Existing Client', 'Offline-Webinar', 'NVF')
      AND csr.rppobjectid IS NULL
    ORDER BY 
        prpp.patient_ref_id,
        prpp.due_date DESC
) latest
WHERE latest.due_date <= CURRENT_DATE -1
ORDER BY latest.due_date ASC;

"""

PLAN_QUERY = """
WITH filtered_rpp AS (
    SELECT *
    FROM public.patient_rpp_registration
    WHERE enrollment_date::date >= date_trunc('month', CURRENT_DATE) - INTERVAL '12 months'
      AND enrollment_date::date <= CURRENT_DATE
),

latest_roles AS (
    SELECT DISTINCT ON (pa.patient_id, pra.assigned_to_role_name)
        pa.patient_id,
        pra.assigned_to_role_name,
        pra.assigned_to_name
    FROM public.patient_rpp_assignment pra
    JOIN public.patient_appointment pa
        ON pa.patient_rpp_id = pra.patient_rpp_id
    WHERE pra.assigned_to_role_name IN ('Psychologist','Psychiatrist','Counsellor')
    ORDER BY pa.patient_id, pra.assigned_to_role_name, pra.date_created DESC
),

role_pivot AS (
    SELECT
        patient_id,
        MAX(CASE WHEN assigned_to_role_name='Psychologist' THEN assigned_to_name END) AS psychologist_name,
        MAX(CASE WHEN assigned_to_role_name='Psychiatrist' THEN assigned_to_name END) AS psychiatrist_name,
        MAX(CASE WHEN assigned_to_role_name='Counsellor' THEN assigned_to_name END) AS counsellor_name
    FROM latest_roles
    GROUP BY patient_id
),

diagnosis_data AS (
    SELECT DISTINCT ON (patient_id)
        patient_id,
        diagnosis_name,
        primary_diagnosis,
        date_updated
    FROM public.patient_provision_diagnosis_treatment
    ORDER BY patient_id, date_updated DESC NULLS LAST
),

appointment_flag AS (
    SELECT DISTINCT patient_id, TRUE AS has_appointment
    FROM public.patient_appointment
    WHERE appointment_time_slot IS NOT NULL
      AND appointment_time_slot <> ''
),

plan_history AS (
    SELECT
        pp.*,
        LAG(pp.enrollment_date::date) OVER (PARTITION BY patient_id ORDER BY enrollment_date::date) AS prev_enrollment,
        LAG(pp.due_date::date) OVER (PARTITION BY patient_id ORDER BY enrollment_date::date) AS prev_due,
        COUNT(*) OVER (PARTITION BY patient_id ORDER BY enrollment_date::date) AS months_with_us
    FROM public.patient_rpp_registration pp
)

SELECT
    patient_id,
    gender_name,
    hosp_name,
    mobile_number::bigint,
    patient_name,
    lead_source,
    marketing_person_name,
    psychologist_name,
    psychiatrist_name,
    counsellor_name,
    enrollment_date::date,
    due_date::date,
    plan_status,
    direct_after_opd,
    patient_ref_id::bigint,
    months_with_us::bigint,
    primary_diagnosis,
    package_diagnosis_name,
    patient_type,
    induction_done,
    amount
FROM (
    SELECT
        pr.patient_id,
        pr.gender_name,
        pp.hosp_name,
        pr.mobile_number,
        pr.patient_name,
        pr.lead_source,
        pr.marketing_person_name,
        rp.psychologist_name,
        rp.psychiatrist_name,
        rp.counsellor_name,
        pp.patient_ref_id,
        pp.enrollment_date,
        pp.due_date,
        COALESCE(
            dd.primary_diagnosis,
            (SELECT string_agg(trim(both E' \n\t\r' from elem), ', ')
             FROM jsonb_array_elements_text(dd.diagnosis_name) AS elem)
        ) AS primary_diagnosis,
        pp.package_diagnosis_name,
        pp.months_with_us,
        pr.induction_done,
        pp.amount,

        CASE
            WHEN pp.prev_enrollment IS NULL THEN 'NEW PLAN'
            WHEN pp.enrollment_date::date <= pp.prev_due THEN 'RENEWAL'
            WHEN pp.enrollment_date::date <= pp.prev_due + INTERVAL '45 days'
                THEN 'LATE RENEWAL'
            ELSE 'REVIVAL'
        END AS plan_status,

        CASE
            WHEN pp.prev_enrollment IS NULL
                 AND af.has_appointment IS NULL
            THEN 'Direct Plan'
            WHEN pp.prev_enrollment IS NULL
                 AND af.has_appointment = TRUE
            THEN 'After OPD'
        END AS direct_after_opd,

        CASE
            WHEN pr.lead_source = 'Corporate' THEN 'Corporate'
            WHEN pr.lead_source = 'NTPC' THEN 'CSR'
            WHEN pr.lead_source = 'CSR' AND pp.amount = 0 THEN 'CSR'
            WHEN pr.lead_source = 'Existing Client' AND pp.amount = 0 THEN 'CSR'
            WHEN pr.csr_id IS NULL OR pr.csr_id = 'regular' THEN 'Regular'
            ELSE 'CSR'
        END AS patient_type,

        ROW_NUMBER() OVER (
            PARTITION BY pr.mobile_number, pp.enrollment_date::date
            ORDER BY pp.enrollment_date::date DESC
        ) AS rn

    FROM public.patient_registration pr
    JOIN plan_history pp
        ON pr.patient_id = pp.patient_id

    LEFT JOIN role_pivot rp
        ON rp.patient_id = pr.patient_id

    LEFT JOIN diagnosis_data dd
        ON dd.patient_id = pr.patient_id

    LEFT JOIN appointment_flag af
        ON af.patient_id = pr.patient_id

    LEFT JOIN public.patient_csr_terms csr
        ON pp._id = csr.rppobjectid

    WHERE
        (
            pr.is_nvf_facility = 'FALSE'
            OR pr.is_nvf_support_revoked = 'TRUE'
            OR EXISTS (
                SELECT 1
                FROM public.patient_rpp_registration rpp_chk
                WHERE rpp_chk.patient_id = pr.patient_id
            )
        )
        AND LOWER(pr.patient_name) NOT LIKE 'test%'
        AND LOWER(pr.patient_name) NOT LIKE '%test'
) t
WHERE rn = 1
AND enrollment_date::date >= date_trunc('month', CURRENT_DATE) - INTERVAL '12 months'
AND enrollment_date::date <= CURRENT_DATE;
"""

OPD_QUERY = """ SELECT *
FROM (
    SELECT
        pr.patient_name,
        pr.hosp_name,
        pr.lead_source,
        pr.patient_id,
        pr.patient_ref_id,
        pr.amount,
        pa.assigned_to_name,
        pa.assigned_to_role_name,
        pr.mobile_number,
        pa.appointment_date::date AS opd_date,
        TO_CHAR(pa.appointment_date::date, 'Mon-YY') AS opd_month,
        CASE
            WHEN prev.patient_id IS NOT NULL THEN 'OLD OPD'
            ELSE 'NEW OPD'
        END AS opd_status,
        CASE
            WHEN pp.suggest_emoneeds_rpp = TRUE  THEN 'Yes'
            WHEN pp.suggest_emoneeds_rpp = FALSE THEN 'No'
            ELSE NULL
        END AS is_suggest_RPP,
        'Regular' AS patient_type,
        pr.status,
        pr.age,
        pr.gender_name,
        pr.family_type_name,
        pr.socio_economic_status_name,
        pr.marital_status_name,
        pr.occupation,
        pr.edu_name,
        pr.state_id,
        pr.district_id,
        ROW_NUMBER() OVER (
            PARTITION BY pa._id
            ORDER BY pp.suggest_emoneeds_rpp DESC NULLS LAST
        ) AS rn
    FROM public.patient_registration pr
    LEFT JOIN public.patient_appointment pa
        ON pr.patient_id = pa.patient_id
    LEFT JOIN public.patient_prescription pp
        ON pr.patient_id = pp.patient_id
    LEFT JOIN (
        SELECT DISTINCT patient_id, appointment_date::date AS appointment_date
        FROM public.patient_appointment
        WHERE appointment_time_slot <> ''
    ) prev
        ON prev.patient_id = pa.patient_id
        AND prev.appointment_date < pa.appointment_date::date
    LEFT JOIN public.patient_csr_terms csr
        ON csr.appointmentobjectid = pa._id
    WHERE pa.appointment_date::date >= date_trunc('month', CURRENT_DATE)::date - INTERVAL '12 months'
      AND pa.appointment_date::date <= CURRENT_DATE
      AND pa.appointment_time_slot <> ''
      AND pa.appointment_status IN (1,5)
      AND pr.lead_source NOT IN ('CSR', 'Existing Client', 'Offline-Webinar', 'NVF')
      AND csr.appointmentobjectid IS NULL
) t
WHERE rn = 1
ORDER BY opd_date ASC;
"""
SESSION_QUERY = """
SELECT 
    dr.slot_date::date,
    dr.task_type,
    dr.is_active,
    dr.booked,
    dr.user_id,
    pr.patient_id,
    pr.hosp_name,       
    pr.patient_name,         
    pr.mobile_number,       
    pr.lead_source,
    CASE
        WHEN pr.csr_id IS NULL OR pr.csr_id = 'regular' THEN 'Regular'
        WHEN pr.lead_source IN ('Kellton', 'Tata Aia', 'TATA AIA', 'Primus') THEN 'Corporate'
        ELSE 'NTPC'
    END AS patient_type,
    COUNT(*) AS total_sessions

FROM public.doctor_roster dr

LEFT JOIN (
    SELECT DISTINCT ON (roster_id) roster_id, patient_id  
    FROM public.patient_roster_mapping
) prm 
ON dr.roster_id = prm.roster_id

LEFT JOIN (
    SELECT DISTINCT ON (patient_id) 
        patient_id, 
        csr_id, 
        lead_source, 
        patient_name, 
        mobile_number,
        hosp_name          
    FROM public.patient_registration
) pr 
ON prm.patient_id = pr.patient_id

WHERE dr.booked = 1                          
  AND dr.is_active = 1                      
  AND dr.task_type ='RPP'    
  AND dr.slot_date::date BETWEEN '2026-01-01' AND CURRENT_DATE

GROUP BY 
    dr.slot_date,
    dr.task_type,
    dr.is_active,
    dr.booked,
    dr.user_id,
    pr.patient_id, 
    pr.hosp_name,       
    pr.patient_name,        
    pr.mobile_number,        
    pr.csr_id,
    pr.lead_source

ORDER BY 
    dr.slot_date DESC,
    dr.user_id,
    dr.task_type;

"""
PLAN_TYPE_QUERY = """WITH classified AS (
    SELECT
        prpp.*,
        CASE
            WHEN LAG(prpp.due_date::date) OVER w IS NULL
                THEN 'New Plan'
            WHEN prpp.enrollment_date::date <= LAG(prpp.due_date::date) OVER w + INTERVAL '45 days'
                THEN 'Renewal'
            ELSE 'Revival'
        END AS plan_type,
        (
            (EXTRACT(YEAR FROM AGE(prpp.due_date::date, prpp.enrollment_date::date)) * 12)
          + EXTRACT(MONTH FROM AGE(prpp.due_date::date, prpp.enrollment_date::date))
        ) AS plan_months
    FROM public.patient_rpp_registration prpp
    WINDOW w AS (PARTITION BY prpp.patient_id ORDER BY prpp.enrollment_date::date)
),
tenure AS (
    SELECT
        patient_id,
        SUM(plan_months) AS total_service_months
    FROM classified
    GROUP BY patient_id
)
SELECT
    pr.patient_name,
    pr.hosp_name,
    pr.lead_source,
    pr.patient_ref_id,
    prpp.amount,
    prpp.package_price,
    prpp.payment_type_name,
    pr.mobile_number,
    prpp.enrollment_date::date,
    prpp.due_date::date,
    pr.age,
    pr.gender_name,
    pr.family_type_name,
    pr.socio_economic_status_name,
    pr.marital_status_name,
    pr.occupation,
    pr.edu_name,
    pr.state_id,
    pr.district_id,
    prpp.renewed,
    prpp.psychologist_name,
    prpp.psychiatrist_name,
    prpp.plan_type,
    t.total_service_months
FROM classified prpp
LEFT JOIN public.patient_registration pr
    ON prpp.patient_id = pr.patient_id
LEFT JOIN tenure t
    ON t.patient_id = prpp.patient_id
WHERE prpp.enrollment_date::date >= date_trunc('month', CURRENT_DATE)::date - INTERVAL '12 months'
  AND pr.lead_source NOT IN ('CSR', 'Existing Client', 'Offline-Webinar', 'NVF')
  AND NOT EXISTS (
        SELECT 1
        FROM public.patient_appointment pa
        JOIN public.patient_csr_terms csr
            ON csr.appointmentobjectid = pa._id
        WHERE pa.patient_id = pr.patient_id
  )"""

