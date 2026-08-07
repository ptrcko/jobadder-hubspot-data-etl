# HubSpot Contact Export
DB is called jobadder-history in parent folder

Exaple command 

python .\hubspot_contact_export.py `                             
>>   "..\jobadder-history" `
>>   --contact-id 59197392 `
>>   --output "..\phase-2-contact-test" 

I think we should think about continuing to use the csv approach.

Can we  please check if
a) We can add a table in the DB for tracking progress
b) Based on the analysis, the kind of batching we need to run 
c) The steps we should go throuhg - suggest we import prior to a cut off date when Hubspot was created, and then carefully handle current activity