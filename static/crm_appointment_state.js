(function(root){
'use strict';

const CANCELLED_STATUSES=new Set(['canceled','cancelled']);
const PARIS_TIME_ZONE='Europe/Paris';
const PARIS_DAY_FORMATTER=new Intl.DateTimeFormat('en-CA',{
 timeZone:PARIS_TIME_ZONE,
 year:'numeric',
 month:'2-digit',
 day:'2-digit',
});
const contactKey=value=>String(value??'');
const belongsToContact=(appointment,contactId)=>contactKey(appointment?.contact_id)===contactKey(contactId);
const hasUsableDate=appointment=>Boolean(
 appointment&&appointment.start_time
 &&!CANCELLED_STATUSES.has(String(appointment.status||'active').toLowerCase())
 &&Number.isFinite(Date.parse(appointment.start_time))
);
const parisDayKey=value=>{
 const date=value instanceof Date?value:new Date(value);
 if(!Number.isFinite(date.getTime()))return '';
 const parts=Object.fromEntries(
  PARIS_DAY_FORMATTER.formatToParts(date)
   .filter(part=>part.type!=='literal')
   .map(part=>[part.type,part.value])
 );
 return `${parts.year}-${parts.month}-${parts.day}`;
};
const contactAppointments=(contactId,appointments)=>(appointments||[])
 .filter(item=>belongsToContact(item,contactId)&&hasUsableDate(item));
const nextAppointment=(contactId,appointments,now=Date.now())=>{
 const today=parisDayKey(now);
 const ordered=contactAppointments(contactId,appointments)
  .map(item=>({item,start:Date.parse(item.start_time)}))
  .filter(row=>parisDayKey(row.start)>=today)
  .sort((first,second)=>first.start-second.start);
 return ordered.find(row=>row.start>=now)?.item||ordered[0]?.item||null;
};
const dateLabel=(contactId,appointments,now=Date.now())=>{
 const appointment=nextAppointment(contactId,appointments,now);
 return appointment
  ?`Prochain RDV le ${new Intl.DateTimeFormat('fr-FR',{timeZone:PARIS_TIME_ZONE,day:'2-digit',month:'2-digit',year:'numeric'}).format(new Date(appointment.start_time))}`
  :'Date du RDV non renseignée';
};
const sortContactsByNextAppointment=(contacts,appointments,now=Date.now())=>(contacts||[])
 .map((item,index)=>{
  const appointment=nextAppointment(item?.id,appointments,now);
  return{item,index,start:appointment?Date.parse(appointment.start_time):Number.POSITIVE_INFINITY};
 })
 .sort((first,second)=>first.start===second.start?first.index-second.index:first.start-second.start)
 .map(row=>row.item);
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
 parisDayKey,
 contactAppointments,
 nextAppointment,
 dateLabel,
 sortContactsByNextAppointment,
 replaceContact,
 signature,
};
})(typeof window!=='undefined'?window:globalThis);
