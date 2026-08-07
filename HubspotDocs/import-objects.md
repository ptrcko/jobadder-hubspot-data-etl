Import records for multiple objects
Last updated: July 22, 2026

Available with any of the following subscriptions, except where noted:

All products and plans

Additional subscriptions required for certain features
Import records for multiple objects to add or update data across different objects. You can import records such as companies or deals, as well as activities like calls or meetings, and create associations between them. Depending on how your data is structured, import two objects using a single file or multiple files.

If you’re only importing one object, follow the single object import steps instead.

If you're new to HubSpot and want to transfer your existing CRM data to HubSpot's CRM, you can use self-service transfer to audit and sync your data.

Please note: your account may use personalized names for each object (e.g., account instead of company). This article refers to objects by their HubSpot default names.
Before you get started
Subscription required

A Marketing Hub Starter, Professional, or Enterprise subscription with Marketing Contacts is required to mark imported contacts as marketing.
A Professional or Enterprise subscription is required to import association labels.
Permissions required Import permissions and Edit permissions for the object you're importing are required to import records and activities.


Please note: existing emails, meetings, notes, and tasks cannot be updated via import, regardless of how you import the data.
Prior to importing, review the get started with imports article. 

Import records for multiple objects (single file)
In your HubSpot account, click More, then navigate to Data Management > Data Integration. If More doesn't appear in your account, navigate to Data Management > Data Integration directly. You can also click Import in the top right of any object index page.
In the Import a file section, click Import data.
Click Advanced imports (all objects).
Select the objects (e.g., deals, meetings) you want to import, then click Next.
Select Single file.
Click the Choose how to import [objects] dropdown menu for each object and select one of the following:

Screenshot of the "Choose how to import Contacts" dropdown menu, showing options for creating and/or updating contacts.
Create and update [records]: the import will create new records and activities, as well as identify and update existing records. To create new records or activities, your file must contain the required properties for that object/activity. To update existing records, your file must contain a unique identifier.
Create new [records] only: the import will only create new records and activities. Existing records in the import file will be ignored. To create new records or activities, your file must contain the required properties for that object/activity.
Update existing [records] only: the import will only update existing records. New records or activities in the import file will be ignored. To update existing records, your file must contain a unique identifier.
Click View import property requirements to open a pop-up box and review the required properties.
Configure how you want to import your data:
Upload a file: click choose a file, then select your import file.
Copy and paste: click paste directly from your spreadsheet and paste your data. Data must be copied from a spreadsheet file (e.g., Numbers, Google Sheets).
Language: if you're importing data in a language other than your default language, click the Select the language of the column headers in your file dropdown menu and select the language. Selecting the correct language allows HubSpot to better match your column headers to existing default properties. If there is no match in your selected language, HubSpot will search for an English property to match.
Click Next.
Ensure you’ve mapped the correct unique identifiers:
Record ID: in the Record ID row, click the HubSpot Property dropdown menu and select Record ID. If a row in your file doesn't contain a value for Record ID, a new record will be created.
Email (contacts only): in the Email row, click the HubSpot Property dropdown menu and select Email. The correct one displays a key icon.
Company domain name (companies only): in the Company Domain Name row, click the HubSpot Property dropdown menu and select Company Domain Name. The correct one displays a key icon. The Company Domain Name property only accepts values up to the top-level domain (e.g., .com, .edu).
Custom property that requires unique values (contacts, companies, deals, tickets, and custom objects only): if you’re created a property that requires unique values, map the column to that property in the HubSpot property column. If you’re importing multiple unique value properties, you’ll select which property to use as the identifier on the Details page before finishing your import.
Screenshot of HubSpot import property mapping page. the HubSpot property with "Record ID" is highlighted with an orange box.
Please note: when using certain unique identifiers, the following behavior applies:

If you use Record ID as a unique identifier, it'll supercede any other unique identifiers included in the import.
If you use a custom property that requires unique values as the unique identifier:
For companies, the Company domain name property will no longer require unique values.
For contacts, the Email property will still require unique values.
For contacts, if you use an existing contact's secondary email as the unique identifier, the secondary email will not replace the primary email as long as you do not include the Record ID column in your file. If you include both the secondary email and Record ID in your file, the secondary email will replace the primary email when imported.
Map other properties:
In the desired row, click the HubSpot Property dropdown menu and select a property.
If you don't want to import a row, click the Import As dropdown menu and select Don't import column.
GIF showing how to change the "Import as" and "HubSpot Property" dropdown menus to correct mapped fields.
Please note: when importing meetings, ensure your file includes an owner column that you map to the Activity assigned to property. This mapping is required to track meetings with sales goals. 
If desired, configure the following advanced options or jump to the next step:
Prevent property overwrite: if you're updating existing records, prevent the import from overwriting records’ existing property values for the row (e.g., First name). 
When this is selected for a property, the import will update the property for new records and existing records that have never had a value for the property.
It won't update the property for existing records that have a value or had a value in the past, even if currently empty.
In the Manage existing values column, select the Don't overwrite checkbox.
Import association labels:
In the Import As column, click the dropdown menu and select Association label. Importing a new association label will not overwrite an existing association label. The imported label will be added to the record as an additional association label. Learn how to manually remove an association label from a record.
When importing two objects, the HubSpot property column will automatically populate the object relationship for the objects you're importing (e.g., Contact and Company). If you're importing more than two objects, select the two objects whose relationship the association labels describe.
GIF showing how to use the "Import As" dropdown menu to set a column as an "Association label."
Click Next.
In the Import name text field enter a name.
Select the Consent checkbox to agree that contacts expect to hear from you and that your import file does not include a purchased list. Learn more about HubSpot's acceptable use policy.
If desired, configure the following advanced options or jump to the next step:
Select the unique value property (contacts, companies, deals, tickets, custom objects only): if your file includes multiple unique value properties for one object, you must select a property to use for deduplication.
This option will not appear if you've also included Record ID because it'll automatically supersede the other unique identifiers.
Click the Property to use to find existing [objects] dropdown menu and select the property you want to use to update or deduplicate records.
Create a segment from imported contacts: select the Create a contacts segment from this import checkbox to automatically create a segment of the imported contacts.
Set legal basis for contact processing: if you've turned on data privacy settings in your account, click the Set the legal basis for processing a contact's data dropdown menu and select a legal basis of processing .
Create marketing contacts: select the Set this contact as a marketing contact checkbox so they can be used in marketing tools such as marketing email. Learn more about marketing contacts billing .
Import configuration page showing "Enriched records" as the import name, agreement checked, marketing contacts option, and "Enrich with Breeze Intelligence" checked
Enrich records: select the Enrich records checkbox to enrich contacts and companies with HubSpot enrichment.
Configure date and number formatting:
If you're importing date or date and time properties, click the Date format dropdown menu and confirm how the date values in your spreadsheet are formatted. For date and time properties, click the Time zone dropdown menu to confirm the time zone the property should use when imported.
If you're importing a file with a number property, click the Number format dropdown menu and confirm which country's number format to use for your data.
Click Finish import. The review import page will open and display a timeline of the import's status along with an estimated completion time.
Import records for multiple objects (multiple files)
Subscription required A Professional or Enterprise subscription is required to import association labels.

Start an import.
Select the two desired objects.
Select how you want to import your data.
In the Is your data in one or multiple files? section, click Multiple files.
Select a file for each object.
Click Next.
Indicate which column is included in both files:
Click the Common column headers found in your files dropdown menu and select the name of the common column.
Click the Which object is [common column] the unique key for? dropdown menu, then select the object that the property should be imported to. For example, if you're importing contacts and companies and are using Company name as the common column, select Company to upload this data to company records. Review a sample file.
Map the properties.
Enter the name of your import and configure optional settings.
Click Finish import. The review import page will open and display a timeline of the import's status along with an estimated completion time.
Manage errors during mapping
Depending on the error, resolve it within the mapping stage of the import process.  

On the Map columns in your file screen, click [x] errors to learn how you can resolve them.
Image of an import error on a mapped field.
In the right panel:
Review the error details.
Click Edit errored values and then select from the following:
Enter or select a replacement value for each row.
Click the verticalMenuIcon three vertical dots icon and click Don't import this value to stop import of the selected value.
Click Done when you're finished.
Proceed with the rest of the import.