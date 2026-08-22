(function(root){
'use strict';

const CANCELLED_STATUSES=new Set(['canceled','cancelled']);
const contactKey=value=>String(value??'');
const belongsToContact=(appointment,contactId)=>contactKey(appointment?.contact_id)===contactKey(contactId);
const hasUsableDate=appointment=>Boolean(
 appointment&&appointment.start_time
 &&!CANCELLED_STATUSES.has(String(appointment.status||'active').toLowerCase())
 &&Number.isFinite(Date.parse(appointment.start_time))
);
const contactAppointments=(contactId,appointments)=>(appointments||[])
 .filter(item=>belongsToContact(item,contactId)&&hasUsableDate(item));
const nextAppointment=(contactId,appointments,now=Date.now())=>{
 const ordered=contactAppointments(contactId,appointments)
  .map(item=>({item,start:Date.parse(item.start_time)}))
  .sort((first,second)=>first.start-second.start);
 return ordered.find(row=>row.start>=now)?.item||ordered[ordered.length-1]?.item||null;
};
const dateLabel=(contactId,appointments,now=Date.now())=>{
 const appointment=nextAppointment(contactId,appointments,now);
 return appointment
  ?new Intl.DateTimeFormat('fr-FR',{timeZone:'Europe/Paris',day:'2-digit',month:'2-digit',year:'numeric'}).format(new Date(appointment.start_time))
  :'Date du RDV non renseignée';
};
const appointmentIdentity=(appointment,index)=>String(
 appointment?.id||appointment?.invitee_uri||appointment?.event_uri||`${appointment?.start_time||''}:${index}`
);
const replaceContact=(appointments,contactId,replacement)=>{
 const others=(appointments||[]).filter(item=>!belongsToContact(item,contactId));
 const unique=new Map();
 (replacement||[]).forEach((item,index)=>{
  if(!item||typeof item!=='object')return;
  const normalized={...item,contact_id:contactKey(item.contact_id||contactId)};
  unique.set(appointmentIdentity(normalized,index),normalized);
 });
 return [...others,...unique.values()];
};
const signature=appointments=>(appointments||[])
 .map((item,index)=>[
  appointmentIdentity(item,index),
  contactKey(item?.contact_id),
  String(item?.start_time||''),
  String(item?.status||'active').toLowerCase(),
  String(item?.response_status||''),
  String(item?.updated_at||''),
 ].join('\u001f'))
 .sort()
 .join('\u001e');

root.CRMAppointmentState={
 CANCELLED_STATUSES,
 belongsToContact,
 hasUsableDate,
 contactAppointments,
 nextAppointment,
 dateLabel,
 replaceContact,
 signature,
};
})(typeof window!=='undefined'?window:globalThis);
