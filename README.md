# Project 4 - Conference

## Task 1

Session implementation
Each entity of the session kind is created with it’s parent conference as an ancestor. This allows efficient access to it’s parent conference using the ancestor relationship. As the ancestor relationship is permanent this means that it will not be possible to move sessions between conferences. 

The duration is an integer property in minutes to allow for easy comparison on length of session. Highlights are implemented as a repeated field to allow them to be easily used as search tags for sessions.
Speaker is implemented as a string of the speakers name.

## Task 3

Two additional queries were implemented:
getShortSessions: This allows a search for all sessions that with a duration shorter than the defined length. 
checkSessionHighlights: This allows for a search on all sessions for a specific highlight topic of interest

The datastore is not able to process a query with an inequality on more than one property at a time. To workaround this limitation you can query with an inequality filter on one property from the datastore and then filter the results programatically on the other type using python. I have implemented this in getLateNonWorkshops by querying the dataatore for sessions starting after 1900 and then fitlering out results where workshop is the session type. If this query was likely to be repeated frequently it may be worth adding an additional porperty to the session kind so that sessions that matched this query could be flagged as such. 

